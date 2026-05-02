"""Génération et validation d'emails nominatifs à partir du dirigeant + domaine.

Toutes les validations SMTP passent par le service VPS distant (cf.
`smtp_verifier.py` et `remote_verify` ci-dessous). Le code SMTP local
(`smtplib` + DNS direct) a été retiré car il ne tournait jamais en pratique
(port 25 bloqué côté ISP).

Étapes :
1. Extrait le domaine depuis le site web
2. Génère des patterns d'emails probables (prenom.nom@, p.nom@, etc.)
3. Valide via le service VPS (RCPT TO réel)

Confidence :
- 'high'   : SMTP confirme que la boîte existe et le domaine n'est pas catchall
- 'medium' : catchall ou MX-only (validation impossible)
- 'low'    : pattern généré mais aucun signal positif
- 'none'   : pas de domaine exploitable
"""

import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, quote_plus

import requests

try:
    from config import VERIFIER_URL, VERIFIER_KEY
except ImportError:
    VERIFIER_URL = ""
    VERIFIER_KEY = ""


# Domaines d'email grand public : on ne peut pas deviner les patterns
# (gmail, yahoo, etc. n'ont rien à voir avec l'entreprise)
PUBLIC_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.fr",
    "hotmail.com", "hotmail.fr", "outlook.com", "outlook.fr",
    "live.com", "live.fr", "msn.com", "orange.fr", "wanadoo.fr",
    "free.fr", "laposte.net", "sfr.fr", "bbox.fr", "numericable.fr",
    "icloud.com", "me.com", "mac.com", "aol.com", "aol.fr",
    "protonmail.com", "proton.me", "gmx.fr", "gmx.com",
}


def _normalize_token(s):
    """Retire accents, ponctuation, passe en minuscule."""
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Ne garder que les lettres
    return re.sub(r"[^a-zA-Z]", "", ascii_str).lower()


def _extract_domain(site_web):
    """Wrapper qui adapte `validators.extract_domain` à l'ancien contrat
    (retourne "" au lieu de None pour faciliter le `if not domain`)."""
    from validators import extract_domain
    return extract_domain(site_web) or ""


def generate_patterns(prenom, nom, domain):
    """Génère les patterns d'email les plus probables, par ordre de priorité.

    Retourne une liste d'emails sans duplicats, triée du plus au moins probable.
    """
    p = _normalize_token(prenom)
    n = _normalize_token(nom)
    if not domain or (not p and not n):
        return []

    patterns = []
    # Ordre de priorité pour le B2B FR
    if p and n:
        patterns.append("{}.{}@{}".format(p, n, domain))           # prenom.nom
        patterns.append("{}{}@{}".format(p[0], n, domain))         # pnom
        patterns.append("{}.{}@{}".format(p[0], n, domain))        # p.nom
        patterns.append("{}{}@{}".format(p, n, domain))            # prenomnom
        patterns.append("{}-{}@{}".format(p, n, domain))           # prenom-nom
        patterns.append("{}.{}@{}".format(n, p, domain))           # nom.prenom
        patterns.append("{}_{}@{}".format(p, n, domain))           # prenom_nom
    if p:
        patterns.append("{}@{}".format(p, domain))                 # prenom
    if n:
        patterns.append("{}@{}".format(n, domain))                 # nom

    # Déduplication en préservant l'ordre
    seen = set()
    uniq = []
    for e in patterns:
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    return uniq


def remote_verify(email, timeout=12):
    """Vérifie un email via le micro-service distant (VPS avec port 25 ouvert).

    Retourne un dict {status: 'valid'|'invalid'|'catchall'|'no_mx'|'unknown', ...}
    ou None si le service n'est pas configuré ou injoignable.
    """
    if not VERIFIER_URL or not VERIFIER_KEY:
        return None
    try:
        resp = requests.get(
            VERIFIER_URL + "/verify",
            params={"email": email, "key": VERIFIER_KEY},
            timeout=timeout,
        )
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException:
        return None
    return None


def find_nominative_email(prenom, nom, site_web):
    """Trouve le meilleur email nominatif pour un dirigeant via le service VPS.

    Retourne {email, confidence, method} avec :
    - 'high'   : SMTP confirmé valide
    - 'medium' : catchall (tout accepte)
    - 'low'    : best guess (aucun signal positif)
    - 'none'   : domaine inexploitable (public, pas de MX, format invalide)
    """
    domain = _extract_domain(site_web)
    if not domain:
        return {"email": "", "confidence": "none", "method": "no_domain"}
    if domain in PUBLIC_EMAIL_DOMAINS:
        return {"email": "", "confidence": "none", "method": "public_domain"}

    patterns = generate_patterns(prenom, nom, domain)
    if not patterns:
        return {"email": "", "confidence": "none", "method": "no_domain"}

    if not (VERIFIER_URL and VERIFIER_KEY):
        # Aucun moyen de valider : on retourne le pattern probable en best-guess
        return {"email": patterns[0], "confidence": "low", "method": "best_guess"}

    # Première passe sur le pattern le plus probable
    first = remote_verify(patterns[0])
    if first is None:
        return {"email": patterns[0], "confidence": "low", "method": "best_guess"}
    status = first.get("status", "")
    if status == "no_mx":
        return {"email": "", "confidence": "none", "method": "no_mx"}
    if status == "invalid_format":
        return {"email": "", "confidence": "none", "method": "no_domain"}
    if status == "catchall":
        return {"email": patterns[0], "confidence": "medium", "method": "catchall"}
    if status == "valid":
        return {"email": patterns[0], "confidence": "high", "method": "smtp_verified"}

    # status == "invalid" ou "unknown" : tester les patterns suivants
    for pattern in patterns[1:5]:
        r = remote_verify(pattern)
        if r is None:
            break
        s = r.get("status", "")
        if s == "valid":
            return {"email": pattern, "confidence": "high", "method": "smtp_verified"}
        if s == "catchall":
            return {"email": pattern, "confidence": "medium", "method": "catchall"}

    return {"email": patterns[0], "confidence": "low", "method": "best_guess"}


def validate_email(email):
    """Valide un email scrapé via le service SMTP distant.

    Retourne un dict : {status, confidence}
      - status : 'valid' | 'catchall' | 'invalid' | 'no_mx' | 'unknown' | 'public'
      - confidence : 'high' | 'medium' | 'low' | 'none'
    """
    if not email or "@" not in email:
        return {"status": "invalid", "confidence": "none"}

    email = email.strip().lower()
    domain = email.split("@", 1)[1]

    # Domaines publics : impossibles à valider fiablement (gmail etc.
    # acceptent tout côté MX mais ça ne prouve pas l'existence)
    if domain in PUBLIC_EMAIL_DOMAINS:
        return {"status": "public", "confidence": "medium"}

    # Passage par le VPS
    if VERIFIER_URL and VERIFIER_KEY:
        result = remote_verify(email)
        if result is not None:
            s = result.get("status", "unknown")
            if s == "valid":
                return {"status": "valid", "confidence": "high"}
            if s == "catchall":
                return {"status": "catchall", "confidence": "medium"}
            if s == "invalid":
                return {"status": "invalid", "confidence": "none"}
            if s == "no_mx":
                return {"status": "no_mx", "confidence": "none"}
            return {"status": "unknown", "confidence": "low"}

    # Pas de VPS : on ne peut pas trancher, on garde l'email sans confidence
    return {"status": "unknown", "confidence": "low"}


def validate_scraped_emails(entreprises, progress_callback=None, workers=5):
    """Valide les emails scrapés de chaque entreprise via le VPS SMTP.

    Ajoute deux clés sur chaque entreprise :
      - 'email_status' : valid / catchall / invalid / no_mx / unknown / public
      - 'email_confidence' : high / medium / low / none
    """
    # Grouper par email unique pour ne valider qu'une fois chaque adresse
    unique_emails = set()
    for e in entreprises:
        email = (e.get("emails", "") or "").strip().lower()
        if email:
            unique_emails.add(email)

    total = len(unique_emails)
    if progress_callback:
        progress_callback("Validation SMTP de %d emails uniques..." % total, 0.0)

    results = {}
    completed = [0]

    def _task(email):
        return email, validate_email(email)

    if unique_emails:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_task, email) for email in unique_emails]
            for future in as_completed(futures):
                try:
                    email, result = future.result()
                    results[email] = result
                except Exception:
                    pass
                completed[0] += 1
                if progress_callback:
                    progress_callback(
                        "Validation email %d/%d" % (completed[0], total),
                        completed[0] / max(total, 1),
                    )

    # On drop l'email quand SMTP confirme qu'il n'existe pas (invalid / no_mx
    # / invalid_format). Le status est conservé pour traçabilité ("on a testé,
    # c'est mort") et Phase 2 Perplexity firera quand même puisque
    # _has_quality_email retourne False sur ces statuts.
    DEAD = {"invalid", "no_mx", "invalid_format"}
    for e in entreprises:
        email = (e.get("emails", "") or "").strip().lower()
        if email and email in results:
            status = results[email]["status"]
            e["email_status"] = status
            e["email_confidence"] = results[email]["confidence"]
            if status in DEAD:
                e["emails"] = ""
        else:
            e["email_status"] = ""
            e["email_confidence"] = ""

    if progress_callback:
        progress_callback("Validation emails terminee !", 1.0)

    return entreprises


def enrich_nominative_emails(entreprises, progress_callback=None):
    """Enrichit les entreprises avec un email nominatif dirigeant.

    Prérequis : les entreprises doivent avoir été enrichies au préalable
    par entreprise_enricher (dirigeant_prenom, dirigeant_nom) et avoir un site_web.

    Ajoute deux clés :
      - 'email_dirigeant' : l'email généré ou vérifié (peut être vide)
      - 'email_dirigeant_confidence' : 'high' | 'medium' | 'low' | 'none'
    """
    total = len(entreprises)
    if progress_callback:
        progress_callback("Recherche emails dirigeants sur %d entreprises..." % total, 0.0)

    # Grouper par domaine pour mutualiser les connexions SMTP
    last_domain = None
    last_mx = None

    for idx, entreprise in enumerate(entreprises):
        prenom = entreprise.get("dirigeant_prenom", "")
        nom = entreprise.get("dirigeant_nom", "")
        site_web = entreprise.get("site_web", "")

        if progress_callback:
            progress_callback(
                "Email dirigeant %d/%d : %s" % (idx + 1, total, entreprise.get("nom", "")),
                (idx + 1) / max(total, 1),
            )

        if not prenom and not nom:
            entreprise["email_dirigeant"] = ""
            entreprise["email_dirigeant_confidence"] = "none"
            continue

        result = find_nominative_email(prenom, nom, site_web)
        entreprise["email_dirigeant"] = result["email"]
        entreprise["email_dirigeant_confidence"] = result["confidence"]

        # Petit délai pour ne pas hammerer le même serveur SMTP
        domain = _extract_domain(site_web)
        if domain and domain == last_domain:
            time.sleep(0.5)
        last_domain = domain

    if progress_callback:
        progress_callback("Emails dirigeants termine !", 1.0)

    return entreprises
