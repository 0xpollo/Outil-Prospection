"""Lookup Google Maps pour récupérer le site web officiel d'une entreprise.

Utilisé en fallback quand Perplexity n'a pas trouvé de site (ou a retourné
le site d'un groupe parent).

Réutilise le scraping HTTP de `scraper.py` (rapide, ~2s, pas de Selenium).
Les résultats sont filtrés :
- Par similarité de nom (>= seuil) pour éviter les confusions
- Par validation de site (validate_site_matches_company) pour rejeter les sites
  de groupes parents (ex: GMaps pointe vers renault.fr pour une concession locale)
"""

import json
import logging
import os
import re
import unicodedata

# Réutilise les internes du scraper du projet
from scraper import _create_http_session, _http_fetch_businesses
from validators import (
    is_annuaire_domain,
    validate_site_matches_company,
)

logger = logging.getLogger(__name__)

_COMMUNES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "communes_france.json")

# Index des communes (chargé lazy, 1 fois)
_COMMUNES = None


def _strip_accents_lower(text):
    if not text:
        return ""
    n = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in n if unicodedata.category(c) != "Mn")


def _normalize_key(text):
    """minuscules, sans accents, lettres+chiffres uniquement."""
    return re.sub(r"[^a-z0-9]", "", _strip_accents_lower(text))


def _load_communes():
    global _COMMUNES
    if _COMMUNES is not None:
        return _COMMUNES
    try:
        with open(_COMMUNES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        by_postal = {}
        by_name = {}
        for entry in data:
            label, lat, lng = entry
            m = re.search(r"\((\d{5})\)", label)
            if m:
                cp = m.group(1)
                if cp not in by_postal:
                    by_postal[cp] = (lat, lng)
            nom = re.sub(r"\s*\(\d{5}\).*", "", label).strip()
            nom_norm = _normalize_key(nom)
            if nom_norm and nom_norm not in by_name:
                by_name[nom_norm] = (lat, lng)
        _COMMUNES = {"by_postal": by_postal, "by_name": by_name}
        logger.debug("GMaps communes : %d CP, %d noms", len(by_postal), len(by_name))
    except Exception as e:
        logger.warning("Impossible de charger %s : %s", _COMMUNES_FILE, e)
        _COMMUNES = {"by_postal": {}, "by_name": {}}
    return _COMMUNES


def _resolve_coords(lieu):
    """(lat, lng) depuis une adresse / lieu textuel.
    Cherche d'abord un code postal (5 chiffres), puis un nom de ville.
    """
    if not lieu:
        return None, None
    communes = _load_communes()

    cp_match = re.search(r"\b(\d{5})\b", lieu)
    if cp_match and cp_match.group(1) in communes["by_postal"]:
        return communes["by_postal"][cp_match.group(1)]

    # Nom de ville : prendre les tokens >= 3 chars et les essayer
    no_accents = _strip_accents_lower(lieu)
    cleaned = re.sub(r"[^a-z\s-]", " ", no_accents)
    tokens = [t for t in cleaned.split() if len(t) >= 3]
    if not tokens:
        return None, None

    # Tester nom complet en 1ʳᵉ tentative
    full_norm = _normalize_key(" ".join(tokens))
    if full_norm in communes["by_name"]:
        return communes["by_name"][full_norm]
    # Token le plus long (souvent le nom de la ville)
    biggest = sorted(tokens, key=len, reverse=True)[0]
    biggest_norm = _normalize_key(biggest)
    if biggest_norm in communes["by_name"]:
        return communes["by_name"][biggest_norm]
    # Préfixe : "NICE CENTRE" → "NICE"
    for nom_norm, coords in communes["by_name"].items():
        if biggest_norm.startswith(nom_norm) and len(nom_norm) >= 4:
            return coords

    return None, None


def _name_similarity(scraped_name, target_name):
    """0.0–1.0 selon le chevauchement de tokens >= 4 chars."""
    a = _normalize_key(scraped_name)
    b = _normalize_key(target_name)
    if not a or not b:
        return 0.0
    tokens_b = {t for t in re.findall(r"[a-z0-9]+", _strip_accents_lower(target_name)) if len(t) >= 4}
    if not tokens_b:
        return 1.0 if (b in a or a in b) else 0.0
    matches = sum(1 for t in tokens_b if t in a)
    return matches / len(tokens_b)


def find_company_site(nom_entreprise, lieu, min_similarity=0.5):
    """Cherche le site web d'une entreprise via Google Maps.

    Retourne un site_web validé (passe is_annuaire et validate_site_matches_company)
    ou "" si rien de fiable trouvé.
    """
    if not nom_entreprise:
        return ""

    lat, lng = _resolve_coords(lieu)
    if lat is None or lng is None:
        logger.debug("GMaps lookup → coords introuvables pour lieu='%s'", lieu)
        return ""

    session = _create_http_session()
    try:
        results = _http_fetch_businesses(session, nom_entreprise, lat, lng, max_results=5)
    except Exception as e:
        logger.warning("GMaps scraping erreur pour '%s' : %s", nom_entreprise, e)
        return ""

    if not results:
        return ""

    best_site = ""
    best_score = 0.0
    for r in results:
        scraped_nom = r.get("nom", "")
        site = (r.get("site_web", "") or "").strip()
        if not site or is_annuaire_domain(site):
            continue
        score = _name_similarity(scraped_nom, nom_entreprise)
        if score < min_similarity:
            continue
        if score <= best_score:
            continue
        # Validation finale : nom doit matcher le domaine ou la page
        if not validate_site_matches_company(site, nom_entreprise):
            logger.debug("GMaps → site rejeté (validation nom) : %s pour '%s'", site, nom_entreprise)
            continue
        best_score = score
        best_site = site

    if best_site:
        logger.info("GMaps → %s : site=%s (similarité=%.2f)", nom_entreprise, best_site, best_score)
    return best_site
