"""Validation et utilitaires partagés pour l'enrichissement avancé.

Porté depuis le bot France Travail (enricher.py). Contient :
- La liste `_NAME_STOP_WORDS` (mots génériques à ignorer dans les noms d'entreprise)
- `validate_site_matches_company` : tokens du nom doivent matcher le domaine ou la page
- `is_parent_group_site` : détecte un site de groupe parent vs entité locale
- `extract_nom_variants` : variantes de nom de famille pour génération d'email
  (noms composés MARTIN-DUPONT, particules "DE LA TOUR", apostrophes "D'ARTAGNAN")
- `is_generic_email` : contact@, info@, etc.
- Blacklists d'annuaires utilisées par Perplexity et le scraping
"""

import re
import unicodedata
from urllib.parse import urlparse

import requests


# Mots génériques à ignorer dans les noms d'entreprise (formes juridiques + métiers
# trop larges qui matchent les sites de groupes parents au lieu de l'entité locale).
# TODO : enrichir si on détecte d'autres faux positifs (ex: "industrie" rejette
# "L'industrie du verre" → garder ce token discriminant ?).
_NAME_STOP_WORDS = {
    # Formes juridiques (incl. courtes)
    "sa", "sarl", "sas", "eurl", "sasu", "sci", "snc", "scop", "ste",
    "société", "societe", "compagnie", "etablissements", "ets", "drh",
    # Mots de structure / périmètre
    "groupe", "group", "holding", "france", "french", "international",
    "internationale", "europe", "europeenne",
    # Mots métier génériques
    "services", "service", "solutions", "consulting", "conseil", "conseils",
    "entreprise", "entreprises", "partenaires",
    "industrie", "industries", "industriel", "industrielle",
    "automobile", "automobiles", "auto", "autos",
    "transport", "transports", "transportation",
    "logistique", "logistiques", "logistics",
    "distribution", "distri",
    "batiment", "construction", "constructions", "travaux",
    "alimentation", "alimentaire", "agro", "agroalimentaire",
    "concession", "concessions", "concessionnaire",
    "negoce", "negocie",
    # Petits mots
    "et", "de", "du", "des", "la", "le", "les", "aux",
}


# Particules de noms de famille à reconnaître pour générer les variantes
_NOM_PARTICULES = {
    "de", "du", "des", "la", "le", "les",
    "von", "van", "der", "den", "ter",
    "el", "al", "ben", "bin", "da", "di", "dos", "do",
}


# Annuaires à blacklister (Perplexity ne doit pas les retourner comme site officiel).
# Format pour `search_domain_filter` Perplexity : préfixe "-" pour exclure.
ANNUAIRE_BLACKLIST = [
    "-entreprises.lefigaro.fr",
    "-fr.mappy.com",
    "-pagesjaunes.fr",
    "-bilansgratuits.fr",
    "-corporama.com",
    "-bodacc.fr",
    "-score3.fr",
]

# Variante plus légère pour la recherche dirigeant (laisse manageo, verif,
# dirigeant.com qui contiennent souvent les noms).
ANNUAIRE_BLACKLIST_DIRIGEANT = [
    "-entreprises.lefigaro.fr",
    "-fr.mappy.com",
    "-pagesjaunes.fr",
]

# Substrings à rejeter dans les domaines retournés par Perplexity / scraping
# (annuaires + réseaux sociaux qui ne sont pas le site officiel).
ANNUAIRE_DOMAIN_SUBSTRINGS = [
    "pagesjaunes.fr", "pages-jaunes.fr",
    "societe.com", "pappers.fr", "infogreffe.fr", "manageo.fr",
    "bodacc.fr", "verif.com", "dirigeant.com", "corporama.com",
    "bilansgratuits.fr", "kompass.com", "score3.fr",
    "entreprises.lefigaro.fr", "fr.mappy.com",
    "facebook.com", "instagram.com", "linkedin.com",
    "youtube.com", "twitter.com", "x.com", "tiktok.com",
]


# Préfixes d'emails génériques (à ne jamais retourner comme email dirigeant)
GENERIC_PREFIXES = [
    "contact", "info", "accueil", "hello", "bonjour",
    "administration", "admin", "commercial", "direction",
    "rh", "recrutement", "secretariat", "comptabilite",
    "support", "service", "agence",
]


# User-Agent unique pour toutes les requêtes HTTP du module
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT}


def _strip_accents(text):
    if not text:
        return ""
    n = unicodedata.normalize("NFD", text)
    return "".join(c for c in n if unicodedata.category(c) != "Mn")


def extract_name_tokens(nom_entreprise):
    """Tokens significatifs du nom (>= 3 chars, hors stop-words).
    Seuil 3 chars : récupère acronymes courts (A68, MSF, IBM) sans le bruit.
    """
    if not nom_entreprise:
        return []
    no_accents = _strip_accents(nom_entreprise.lower())
    tokens = re.split(r"[^a-z0-9]+", no_accents)
    return [t for t in tokens if len(t) >= 3 and t not in _NAME_STOP_WORDS]


def extract_domain(site_web):
    """Extrait le domaine (sans www., sans port, sans path) d'une URL."""
    if not site_web:
        return None
    if "://" not in site_web:
        site_web = "http://" + site_web
    try:
        parsed = urlparse(site_web)
        host = (parsed.netloc or parsed.path).lower().strip()
        host = re.sub(r"^www\.", "", host)
        host = host.split(":")[0].split("/")[0]
        if not host or "." not in host:
            return None
        return host
    except Exception:
        return None


def is_annuaire_domain(site_web):
    """True si l'URL pointe vers un annuaire connu ou un réseau social."""
    domain = extract_domain(site_web)
    if not domain:
        return False
    return any(sub in domain for sub in ANNUAIRE_DOMAIN_SUBSTRINGS)


def validate_site_matches_company(site_web, nom_entreprise, timeout=5):
    """Vérifie qu'un site correspond à l'entreprise.

    1. Match lexical sur le slug du domaine (gratuit)
    2. Fallback : fetch les premiers 8ko de la page et cherche un token

    Retourne True si au moins un token significatif matche, False sinon.
    Si pas de token discriminant : True (on ne peut pas invalider).
    """
    domain = extract_domain(site_web)
    if not domain or not nom_entreprise:
        return False

    # Rejet d'office des annuaires
    if any(sub in domain for sub in ANNUAIRE_DOMAIN_SUBSTRINGS):
        return False

    tokens = extract_name_tokens(nom_entreprise)
    if not tokens:
        return True  # pas de token discriminant → on accepte par défaut

    # Slug du domaine (sans TLD, sans tirets/points)
    domain_slug = re.sub(r"[^a-z0-9]", "", domain.rsplit(".", 1)[0])

    # 1) Match lexical sur le domaine
    for token in tokens:
        if token in domain_slug:
            return True

    # 2) Fallback : scrape les premiers 8 ko et cherche un token dans le texte
    try:
        url = site_web if "://" in site_web else "http://" + site_web
        resp = requests.get(url, timeout=timeout, headers=HEADERS, stream=True)
        if resp.status_code != 200:
            return False
        head_html = ""
        for chunk in resp.iter_content(chunk_size=4096, decode_unicode=True):
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8", errors="ignore")
            head_html += chunk
            if "</head>" in head_html.lower() or len(head_html) > 8192:
                break
        page_text = re.sub(r"<[^>]+>", " ", head_html).lower()
        page_text = _strip_accents(page_text)
        for token in tokens:
            if token in page_text:
                return True
    except Exception:
        pass

    return False


def is_parent_group_site(site_web, nom_entreprise):
    """Détecter un site de groupe parent (ex: renault.fr pour BERTHIAND AUTOMOBILES).

    Signal : le nom a >= 2 tokens significatifs mais le domaine n'en matche
    qu'une partie (le constructeur, pas la concession locale).
    """
    domain = extract_domain(site_web)
    if not domain:
        return False
    tokens = extract_name_tokens(nom_entreprise)
    if len(tokens) < 2:
        return False
    domain_slug = re.sub(r"[^a-z0-9]", "", domain.rsplit(".", 1)[0])
    matched = sum(1 for t in tokens if t in domain_slug)
    return 0 < matched < len(tokens)


def extract_nom_variants(nom_brut):
    """Variantes plausibles d'un nom de famille pour génération d'email.

    Gère :
    - Composés "MARTIN-DUPONT" → ["dupont", "martindupont", "martin-dupont", "martin"]
    - Particules "DE LA TOUR" → ["tour", "delatour", "latour"]
    - Apostrophes "D'ARTAGNAN" → ["artagnan", "dartagnan"]
    - Multiples "ISABELLE GONCALVES" (parsing API erroné) → ["goncalves", "isabellegoncalves"]
    """
    if not nom_brut:
        return []

    no_accents = _strip_accents(nom_brut.lower())
    # Apostrophes → espace pour split
    cleaned = re.sub(r"['’‘]", " ", no_accents)
    cleaned = re.sub(r"[^a-z\s-]", " ", cleaned)
    tokens = [t for t in re.split(r"[\s-]+", cleaned.strip()) if t]
    if not tokens:
        return []

    # Tokens significatifs : hors particules + au moins 2 chars
    significant = [t for t in tokens if t not in _NOM_PARTICULES and len(t) >= 2]
    if not significant:
        significant = [t for t in tokens if len(t) >= 2] or tokens

    variants = []

    # 1. Dernier mot significatif (cas le plus probable)
    variants.append(significant[-1])

    # 2. Plusieurs mots significatifs : variantes composées
    if len(significant) > 1:
        joined = "".join(significant)
        hyphenated = "-".join(significant)
        if joined not in variants:
            variants.append(joined)
        if hyphenated not in variants:
            variants.append(hyphenated)
        if significant[0] not in variants:
            variants.append(significant[0])

    # 3. Particules collées : "DE LA TOUR" → "delatour", "latour"
    if len(tokens) > len(significant):
        full_joined = "".join(tokens)
        if full_joined not in variants:
            variants.append(full_joined)
        if len(tokens) >= 2:
            last_two = "".join(tokens[-2:])
            if last_two not in variants and len(last_two) >= 3:
                variants.append(last_two)

    return [v for v in variants if v and len(v.replace("-", "")) >= 2]


def is_generic_email(email):
    """contact@, info@, direction@... (préfixes non personnels)."""
    if not email or "@" not in email:
        return True
    prefix = email.split("@", 1)[0].lower()
    return any(prefix == g or prefix.startswith(g + ".") for g in GENERIC_PREFIXES)


def normalize_for_email(text):
    """Normalise un prénom/nom pour usage en local-part : minuscules, sans accents,
    lettres et tirets uniquement."""
    if not text:
        return ""
    no_accents = _strip_accents(text.lower())
    return re.sub(r"[^a-z-]", "", no_accents)
