"""Scoring 0-100 d'un prospect, dérivé du tier P0/P1/P2/X.

Le tier est lui-même calculé par `email_processor.classify_tier` à partir
du meilleur email disponible (validité × rang destinataire). Le scoring
n'apporte pas de signal additionnel — il fournit juste une valeur
numérique pratique pour trier dans l'UI et l'export Excel.
"""

from email_processor import classify_tier, pick_best_email


# Map tier -> score numérique. Calibré pour conserver le tri intuitif
# A/B/C/D affiché côté UI (Hot/Chaud/Tiède/Froid/Exclu) — mais les seuils
# sont alignés sur les vraies promesses de l'archi v2.
_TIER_SCORE = {
    "P0": 90,   # email valid (ou catchall publié) → Hot
    "P1": 65,   # email publié, signal site, ou perso solo → Chaud
    "P2": 25,   # recherche manuelle → Tiède/Froid
    "X": 0,     # franchise/chaîne → Exclu
}


def calculate_score(entreprise):
    """Score numérique tiré du tier. Le tier est recalculé ici si absent.

    Affinements internes au tier (pour ordonner finement) :
      - P0 : 95 si dirigeant nominatif valid, 90 sinon
      - P1 : 70 si publié corporate, 60 si perso (gmail/...) solo
    """
    tier = entreprise.get("tier") or classify_tier(entreprise)
    base = _TIER_SCORE.get(tier, 10)
    if tier == "P0":
        best = pick_best_email(entreprise)
        if best and best.get("source") in ("pattern",) and \
                int(best.get("destinataire_rank") or 9) == 1:
            return 95
        return 90
    if tier == "P1":
        best = pick_best_email(entreprise)
        if best and not best.get("is_public_domain"):
            return 70
        return 60
    return base


def calculate_scores(entreprises):
    """Ajoute un champ `score` à chaque entreprise. Modifie en place."""
    for e in entreprises:
        e["score"] = calculate_score(e)
    return entreprises


def score_label(score):
    """Label texte (cohérent A/B/C/D pour rétro-compat UI)."""
    if score >= 90:
        return "Hot"
    if score >= 60:
        return "Chaud"
    if score >= 25:
        return "Tiède"
    if score > 0:
        return "Froid"
    return "Exclu"


def score_color(score):
    if score >= 90:
        return "#16a34a"
    if score >= 60:
        return "#84cc16"
    if score >= 25:
        return "#f59e0b"
    if score > 0:
        return "#94a3b8"
    return "#64748b"
