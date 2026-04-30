"""Enrichissement des entreprises via l'API Recherche d'entreprises (data.gouv.fr).

API publique, gratuite, sans clé : https://recherche-entreprises.api.gouv.fr
Source : INSEE Sirene + INPI RNE (dirigeants).

Ajoute pour chaque entreprise :
- siren
- dirigeant_prenom / dirigeant_nom / dirigeant_qualite
- tranche_effectif (code INSEE)
- date_creation_entreprise
- code_naf
- match_confidence ('high', 'medium', 'low', 'none')
"""

import re
import time
import unicodedata
from urllib.parse import quote_plus

import requests
from requests.adapters import HTTPAdapter


API_BASE = "https://recherche-entreprises.api.gouv.fr/search"
REQUEST_TIMEOUT = 10

# L'API limite à 7 req/sec — on laisse une marge
MIN_DELAY_BETWEEN_REQUESTS = 0.15

# Code postal : 5 chiffres consécutifs dans une adresse
_POSTAL_RE = re.compile(r"\b(\d{5})\b")

# Mots à retirer du nom pour faire un match plus permissif
_STOP_WORDS = {
    "le", "la", "les", "du", "de", "des", "au", "aux", "et",
    "sarl", "sas", "sasu", "eurl", "eirl", "sa", "scp", "snc",
    "restaurant", "boulangerie", "pizzeria", "garage", "coiffure",
}


def _create_session():
    """Crée une session HTTP réutilisable."""
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=5, pool_maxsize=5, max_retries=1)
    session.mount("https://", adapter)
    return session


def _normalize_name(nom):
    """Normalise un nom d'entreprise pour la recherche.

    Retire les accents, ponctuation, et ne garde que les mots significatifs.
    """
    if not nom:
        return ""
    # Retirer accents
    nfkd = unicodedata.normalize("NFKD", nom)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Retirer ponctuation, garder lettres/chiffres/espaces
    cleaned = re.sub(r"[^\w\s]", " ", ascii_name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _extract_postal_code(adresse):
    """Extrait le code postal d'une adresse."""
    if not adresse:
        return ""
    m = _POSTAL_RE.search(adresse)
    return m.group(1) if m else ""


def _extract_department(code_postal):
    """Extrait le code département (2 chiffres) du code postal."""
    if not code_postal or len(code_postal) < 2:
        return ""
    # Corse : 2A / 2B
    if code_postal.startswith("20"):
        return "2A" if int(code_postal) < 20200 else "2B"
    return code_postal[:2]


def _pick_best_dirigeant(dirigeants):
    """Sélectionne le dirigeant le plus pertinent (personne physique, qualité prioritaire)."""
    if not dirigeants:
        return None

    # Filtrer les personnes physiques (pas les sociétés)
    personnes = [d for d in dirigeants if d.get("type_dirigeant") == "personne physique"]
    if not personnes:
        return None

    # Qualités prioritaires (décisionnaires)
    priority_order = [
        "président", "presidente", "gérant", "gerante",
        "directeur général", "directrice générale",
        "associé", "associe", "cogérant",
    ]

    for priority in priority_order:
        for p in personnes:
            qualite = (p.get("qualite") or "").lower()
            if priority in qualite:
                return p

    # Sinon, le premier personne physique trouvé
    return personnes[0]


def _match_score(result, nom_recherche, code_postal):
    """Calcule un score de correspondance entre un résultat API et la recherche.

    Retourne (score, confidence) où score est numérique et confidence est str.
    """
    api_nom = _normalize_name(result.get("nom_complet", "")).lower()
    api_enseignes = result.get("siege", {}).get("liste_enseignes") or []
    api_enseignes_norm = " ".join(_normalize_name(e) for e in api_enseignes).lower()

    nom_norm = _normalize_name(nom_recherche).lower()

    # Score par chevauchement de mots significatifs
    nom_words = {w for w in nom_norm.split() if len(w) > 2 and w not in _STOP_WORDS}
    api_words = set(api_nom.split() + api_enseignes_norm.split())

    if not nom_words:
        overlap = 0.0
    else:
        overlap = len(nom_words & api_words) / len(nom_words)

    # Bonus code postal exact
    api_cp = result.get("siege", {}).get("code_postal", "")
    cp_match = (api_cp == code_postal) if code_postal else False

    # Confidence finale
    if overlap >= 0.8 and cp_match:
        confidence = "high"
    elif overlap >= 0.5 and cp_match:
        confidence = "medium"
    elif overlap >= 0.5:
        confidence = "low"
    else:
        confidence = "none"

    return overlap + (0.3 if cp_match else 0), confidence


def _search_entreprise(nom, code_postal, session):
    """Cherche une entreprise via l'API data.gouv.fr.

    Stratégie : d'abord avec code_postal, fallback sur département si rien.
    Retourne (result_dict, confidence) ou (None, 'none').
    """
    nom_clean = _normalize_name(nom)
    if not nom_clean:
        return None, "none"

    # Tentative 1 : nom + code postal (précis)
    if code_postal:
        params = {
            "q": nom_clean,
            "code_postal": code_postal,
            "per_page": 5,
        }
        results = _api_call(params, session)
        if results:
            best = max(results, key=lambda r: _match_score(r, nom, code_postal)[0])
            score, conf = _match_score(best, nom, code_postal)
            if conf != "none":
                return best, conf

    # Tentative 2 : nom + département (plus large)
    dept = _extract_department(code_postal) if code_postal else ""
    if dept:
        params = {
            "q": nom_clean,
            "departement": dept,
            "per_page": 5,
        }
        results = _api_call(params, session)
        if results:
            best = max(results, key=lambda r: _match_score(r, nom, code_postal)[0])
            score, conf = _match_score(best, nom, code_postal)
            if conf != "none":
                # Confidence limitée sans match exact du CP
                if conf == "high":
                    conf = "medium"
                return best, conf

    return None, "none"


def _api_call(params, session):
    """Appelle l'API et retourne la liste des résultats (ou [] en cas d'erreur)."""
    try:
        qs = "&".join("{}={}".format(k, quote_plus(str(v))) for k, v in params.items())
        url = "{}?{}".format(API_BASE, qs)
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return []
        data = resp.json()
        return data.get("results", []) or []
    except Exception:
        return []


def _extract_fields(result, confidence):
    """Extrait les champs utiles d'un résultat API."""
    if not result:
        return {
            "siren": "",
            "dirigeant_prenom": "",
            "dirigeant_nom": "",
            "dirigeant_qualite": "",
            "tranche_effectif": "",
            "date_creation": "",
            "code_naf": "",
            "match_confidence": "none",
        }

    dirigeant = _pick_best_dirigeant(result.get("dirigeants") or [])
    siege = result.get("siege") or {}

    prenom = ""
    nom = ""
    qualite = ""
    if dirigeant:
        # 'prenoms' peut contenir plusieurs prénoms — on garde le premier
        prenoms_raw = dirigeant.get("prenoms") or ""
        prenom = prenoms_raw.split()[0] if prenoms_raw else ""
        nom = dirigeant.get("nom") or ""
        qualite = dirigeant.get("qualite") or ""

    return {
        "siren": result.get("siren") or "",
        "dirigeant_prenom": prenom,
        "dirigeant_nom": nom,
        "dirigeant_qualite": qualite,
        "tranche_effectif": result.get("tranche_effectif_salarie") or "",
        "date_creation_entreprise": result.get("date_creation") or "",
        "code_naf": result.get("activite_principale") or "",
        "match_confidence": confidence,
    }


def enrich_entreprises(entreprises, progress_callback=None):
    """Enrichit chaque entreprise avec les données du RNE (dirigeants, SIREN, etc.).

    Args:
        entreprises: Liste de dicts avec clés 'nom' et 'adresse'
        progress_callback: Fonction appelée avec (message, progression 0-1)

    Returns:
        La même liste avec les champs ajoutés (voir _extract_fields).
    """
    total = len(entreprises)
    if progress_callback:
        progress_callback("Recherche des dirigeants sur %d entreprises..." % total, 0.0)

    session = _create_session()
    last_call = 0.0

    for idx, entreprise in enumerate(entreprises):
        nom = entreprise.get("nom", "")
        adresse = entreprise.get("adresse", "")
        code_postal = _extract_postal_code(adresse)

        if progress_callback:
            progress_callback(
                "Recherche dirigeant %d/%d : %s" % (idx + 1, total, nom),
                (idx + 1) / max(total, 1),
            )

        # Throttling pour respecter la limite 7 req/sec
        elapsed = time.time() - last_call
        if elapsed < MIN_DELAY_BETWEEN_REQUESTS:
            time.sleep(MIN_DELAY_BETWEEN_REQUESTS - elapsed)

        result, confidence = _search_entreprise(nom, code_postal, session)
        last_call = time.time()

        fields = _extract_fields(result, confidence)
        entreprise.update(fields)

    session.close()

    if progress_callback:
        progress_callback("Enrichissement dirigeants termine !", 1.0)

    return entreprises
