"""Recherche d'entreprise et de dirigeant via Perplexity Sonar.

Porté depuis le bot France Travail (enricher.py). Utilise :
- Le modèle `sonar` (rapide, peu cher, ~$0.005/requête)
- `response_format` JSON Schema strict (forcer la structure)
- `search_domain_filter` pour blacklister les annuaires
- `web_search_options.user_location: FR` pour le biais géographique

Cap de coût : timeout 10s + max 2 retries. Si échec, retourne None et le
pipeline d'enrichissement continue avec les autres sources.
"""

import json
import logging
import re
import time
from urllib.parse import urlparse

import requests

from config import PERPLEXITY_API_KEY
from validators import (
    ANNUAIRE_BLACKLIST,
    ANNUAIRE_BLACKLIST_DIRIGEANT,
    is_annuaire_domain,
    is_generic_email,
    validate_site_matches_company,
)

logger = logging.getLogger(__name__)

# Timeout user-spec : 10s sur Perplexity. Le 1er run sur un schéma met parfois
# 10-30s côté API ; on laisse une marge avec retry.
_TIMEOUT = 10
_MAX_RETRIES = 2

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
URL_REGEX = re.compile(r"https?://[a-zA-Z0-9._~:/?#\[\]@!$&'()*+,;=-]+")

# Domaines email à exclure du parsing (faux positifs courants)
EMAIL_BLACKLIST_SUBSTRINGS = [
    "example.com", "noreply", "no-reply", "unsubscribe",
    "privacy", "google.com", "wixpress", "sentry", "webpack",
    ".png", ".jpg", ".gif", ".svg", "cookie", "rgpd",
    "mailtrap", "mailgun", "sendgrid", "test.", "@test",
]


_SYSTEM_COMPANY = (
    "Tu es un assistant de recherche spécialisé dans les coordonnées "
    "d'entreprises françaises. Tu trouves le SITE WEB OFFICIEL de l'entreprise "
    "(pas un annuaire) et les emails publics de contact.\n\n"
    "RÈGLES STRICTES :\n"
    "1. Le site_web_officiel doit être le DOMAINE PROPRE de l'entreprise. "
    "PAS un annuaire.\n"
    "2. Sites à NE JAMAIS retourner comme officiel (annuaires) :\n"
    "   - entreprises.lefigaro.fr, fr.mappy.com, pagesjaunes.fr\n"
    "   - societe.com, pappers.fr, infogreffe.fr, manageo.fr\n"
    "   - bodacc.fr, verif.com, dirigeant.com, corporama.com\n"
    "3. Si tu ne trouves QUE des annuaires, retourne site_web_officiel='' "
    "et site_web_confiance='basse'. MIEUX VAUT VIDE QUE FAUX.\n"
    "4. Confiance haute = page officielle confirmée (about, contact, mentions "
    "légales avec SIRET). Moyenne = domaine cohérent. Basse = doute.\n"
    "5. Ne JAMAIS inventer un email. Champ vide si introuvable.\n\n"
    "Réponds UNIQUEMENT en JSON conforme au schéma."
)

_SYSTEM_DIRIGEANT = (
    "Tu es un expert en recherche de coordonnées professionnelles. "
    "Ta mission est de trouver le NOM COMPLET et l'email PROFESSIONNEL "
    "PERSONNEL d'un dirigeant (pas un email générique contact@/info@).\n\n"
    "RÈGLES STRICTES :\n"
    "1. UN SEUL prénom dans 'prenom' (le prénom usuel, pas de virgule).\n"
    "2. UN SEUL nom de famille dans 'nom'.\n"
    "3. Si plusieurs dirigeants : choisis le PRINCIPAL (gérant > président > "
    "directeur).\n"
    "4. Si tu trouves un nom mais pas d'email perso : retourne le nom + "
    "email_dirigeant=''. NE PAS retourner contact@/info@ comme email_dirigeant.\n"
    "5. Sources préférées : LinkedIn, site officiel (équipe, mentions légales), "
    "pappers.fr, infogreffe.fr.\n"
    "6. Ne JAMAIS inventer.\n\n"
    "Réponds UNIQUEMENT en JSON conforme au schéma."
)

_SCHEMA_COMPANY = {
    "type": "object",
    "properties": {
        "site_web_officiel": {"type": "string"},
        "site_web_confiance": {"type": "string", "enum": ["haute", "moyenne", "basse"]},
        "raison_confiance": {"type": "string"},
        "emails": {"type": "array", "items": {"type": "string"}},
        "telephones": {"type": "array", "items": {"type": "string"}},
        "linkedin": {"type": "string"},
        "urls_consultees": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["site_web_officiel", "site_web_confiance", "emails"],
}

_SCHEMA_DIRIGEANT = {
    "type": "object",
    "properties": {
        "prenom": {"type": "string"},
        "nom": {"type": "string"},
        "email_dirigeant": {"type": "string"},
        "site_web": {"type": "string"},
        "linkedin_profil": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["prenom", "nom"],
}


def is_available():
    """True si la clé API Perplexity est configurée."""
    return bool(PERPLEXITY_API_KEY)


def _extract_emails_from_text(text):
    raw = EMAIL_REGEX.findall(text or "")
    valid = []
    for email in raw:
        email = email.strip().rstrip(".")
        if any(x in email.lower() for x in EMAIL_BLACKLIST_SUBSTRINGS):
            continue
        if email.lower().endswith((".png", ".jpg", ".gif", ".svg", ".css", ".js")):
            continue
        valid.append(email)
    # Dédup en gardant l'ordre
    return list(dict.fromkeys(valid))


def _extract_json(text):
    if not text:
        return None
    try:
        if "```" in text:
            for part in text.split("```")[1:]:
                cleaned = part.strip()
                if cleaned.startswith("json"):
                    cleaned = cleaned[4:]
                cleaned = cleaned.strip()
                if cleaned.startswith("{"):
                    return json.loads(cleaned)
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
    except json.JSONDecodeError:
        pass
    return None


def _call_sonar(query, system_prompt, schema, domain_filter):
    """Appel Perplexity Sonar avec retry sur 429/5xx/timeout."""
    if not PERPLEXITY_API_KEY:
        return None

    payload = {
        "model": "sonar",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query},
        ],
        "temperature": 0.1,
        "max_tokens": 600,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"schema": schema},
        },
        "search_domain_filter": domain_filter,
        "web_search_options": {
            "search_context_size": "low",
            "user_location": {"country": "FR"},
        },
    }

    headers = {
        "Authorization": "Bearer " + PERPLEXITY_API_KEY,
        "Content-Type": "application/json",
    }

    last_error = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.post(
                "https://api.perplexity.ai/chat/completions",
                headers=headers,
                json=payload,
                timeout=_TIMEOUT,
            )
            if resp.status_code in (429, 500, 502, 503, 504):
                logger.warning("Perplexity %s (retry %d/%d)", resp.status_code, attempt + 1, _MAX_RETRIES)
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code != 200:
                logger.warning("Perplexity erreur %s: %s", resp.status_code, resp.text[:200])
                return None
            return resp.json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = e
            logger.warning("Perplexity timeout/réseau (retry %d/%d): %s", attempt + 1, _MAX_RETRIES, e)
            time.sleep(2 * (attempt + 1))
            continue
        except Exception as e:
            logger.warning("Erreur Perplexity: %s", e)
            return None

    logger.warning("Perplexity échec après %d tentatives: %s", _MAX_RETRIES, last_error)
    return None


def _parse_response(api_resp):
    """Extrait JSON + emails regex + URLs depuis la réponse brute Perplexity."""
    try:
        raw_content = api_resp["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as e:
        logger.warning("Perplexity réponse invalide: %s", e)
        return None, ""

    data = _extract_json(raw_content)
    return data, raw_content


def search_company(nom_entreprise, lieu="", siret=""):
    """Recherche les coordonnées d'une entreprise.

    Retourne {emails, telephones, site_web, linkedin, urls_found, site_confiance}
    ou None si Perplexity indisponible / erreur.
    Le site_web a déjà été validé via validate_site_matches_company.
    """
    if not PERPLEXITY_API_KEY or not nom_entreprise:
        return None

    localisation = " à " + lieu if lieu else ""
    siret_hint = " (SIRET : " + siret + ")" if siret else ""
    query = (
        'Trouve les coordonnées complètes de l\'entreprise "' + nom_entreprise + '"' + localisation + siret_hint + '. '
        "Cherche l'adresse email, le téléphone, le site web officiel et la page LinkedIn. "
        "Sources : site officiel (page contact, mentions légales, CGV), "
        "societe.com, pappers.fr, Pages Jaunes, Google Maps, infogreffe.fr. "
        "Donne TOUTES les adresses email trouvées."
    )

    api_resp = _call_sonar(query, _SYSTEM_COMPANY, _SCHEMA_COMPANY, ANNUAIRE_BLACKLIST)
    if api_resp is None:
        return None

    data, raw_content = _parse_response(api_resp)

    emails_json = []
    site_web = ""
    site_confiance = ""
    telephones = []
    linkedin = ""
    urls_consultees = []

    if data:
        emails_json = [e.strip() for e in data.get("emails", []) if isinstance(e, str) and "@" in e]
        site_web = (data.get("site_web_officiel") or data.get("site_web") or "").strip()
        site_confiance = (data.get("site_web_confiance") or "").strip().lower()
        if site_confiance == "basse":
            logger.info("Perplexity → %s : site rejeté (confiance basse: %s)", nom_entreprise, (data.get("raison_confiance") or "")[:80])
            site_web = ""
        telephones = [t.strip() for t in data.get("telephones", []) if t]
        linkedin = (data.get("linkedin", "") or "").strip()
        urls_consultees = data.get("urls_consultees", []) or []

    # Filet de sécurité : extraction regex sur le texte brut
    emails_regex = _extract_emails_from_text(raw_content)
    all_emails = list(dict.fromkeys(emails_json + emails_regex))

    urls_in_text = URL_REGEX.findall(raw_content)
    all_urls = list(dict.fromkeys(urls_consultees + urls_in_text))

    # Si pas de site_web JSON, fallback sur la 1ʳᵉ URL non-annuaire
    if not site_web:
        for url in all_urls:
            try:
                parsed = urlparse(url)
            except Exception:
                continue
            if not parsed.netloc:
                continue
            if is_annuaire_domain(url):
                continue
            if any(x in parsed.netloc for x in ["perplexity", "google", "wikipedia", "francetravail"]):
                continue
            site_web = parsed.scheme + "://" + parsed.netloc
            break

    # Validation finale du site_web : doit matcher l'entreprise
    if site_web and not validate_site_matches_company(site_web, nom_entreprise):
        logger.info("Perplexity → site rejeté (validation nom/domaine) : %s pour '%s'", site_web, nom_entreprise)
        site_web = ""

    found = []
    if all_emails:
        found.append(str(len(all_emails)) + " email(s)")
    if site_web:
        found.append("site=" + site_web)
    logger.info("Perplexity company → %s : %s", nom_entreprise, ", ".join(found) if found else "rien trouvé")

    return {
        "emails": all_emails[:5],
        "telephones": telephones[:3],
        "site_web": site_web,
        "site_confiance": site_confiance,
        "linkedin": linkedin,
        "urls_found": all_urls[:10],
    }


def search_dirigeant(prenom, nom, qualite, nom_entreprise, site_web=""):
    """Recherche ciblée de l'email perso d'un dirigeant connu.

    Retourne {email, site_web} si succès, None sinon.
    """
    if not PERPLEXITY_API_KEY or not (prenom and nom):
        return None

    site_hint = " Le site de l'entreprise est " + site_web + "." if site_web else ""
    query = (
        'Trouve l\'email professionnel personnel de ' + prenom + ' ' + nom + ', '
        + (qualite + ' ' if qualite else '')
        + 'de l\'entreprise "' + nom_entreprise + '".' + site_hint + ' '
        'NE DONNE PAS les emails génériques (contact@, info@, accueil@). '
        'Je cherche son email PERSONNEL (type prenom.nom@ ou p.nom@). '
        'Cherche sur LinkedIn, le site web, les annuaires professionnels.'
    )

    api_resp = _call_sonar(query, _SYSTEM_DIRIGEANT, _SCHEMA_DIRIGEANT, ANNUAIRE_BLACKLIST_DIRIGEANT)
    if api_resp is None:
        return None

    data, raw_content = _parse_response(api_resp)
    if not data:
        # Filet : essayer de récupérer un email perso dans le texte brut
        for e in _extract_emails_from_text(raw_content):
            if not is_generic_email(e):
                logger.info("Perplexity dirigeant (regex fallback) → %s : %s", nom_entreprise, e)
                return {"email": e, "site_web": ""}
        return None

    email_field = (data.get("email_dirigeant") or "").strip()
    site_field = (data.get("site_web") or "").strip()

    # Filtrer l'email s'il est générique
    if email_field and is_generic_email(email_field):
        email_field = ""

    # Validation site_web optionnelle (on ne rejette pas, juste log)
    if site_field and not validate_site_matches_company(site_field, nom_entreprise):
        site_field = ""

    if email_field:
        logger.info("Perplexity dirigeant → %s : trouvé %s", nom_entreprise, email_field)
    elif site_field:
        logger.info("Perplexity dirigeant → %s : pas d'email mais site=%s", nom_entreprise, site_field)
    else:
        logger.info("Perplexity dirigeant → %s : pas d'email perso trouvé", nom_entreprise)

    if not email_field and not site_field:
        return None

    return {"email": email_field, "site_web": site_field}
