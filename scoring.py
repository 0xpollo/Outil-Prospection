"""Scoring de maturité digitale des prospects."""

import re

# --- Détection franchises / groupes / chaînes ---
_FRANCHISE_KEYWORDS = re.compile(
    r"\b("
    # Restauration rapide / chaînes resto
    r"mcdonald|burger king|kfc|subway|domino|pizza hut|starbucks|"
    r"five guys|quick|o'tacos|otacos|hippopotamus|buffalo grill|"
    r"flunch|courtepaille|del arte|la pataterie|popeyes|"
    r"boulangerie paul\b|class'croute|brioche dor[ée]e|marie blach[eè]re|"
    r"columbus caf[eé]|bagel corner|nachos|exki|cojean|"
    # Grande distribution / alimentaire
    r"carrefour|leclerc|auchan|lidl|intermarch[eé]|casino|monoprix|"
    r"franprix|picard|biocoop|grand frais|netto|aldi|cora|"
    r"super u\b|hyper u\b|march[eé] u\b|syst[eè]me u|"
    r"match|g20|proxy|vival|spar\b|8 [àa] huit|"
    # Auto / garage
    r"norauto|midas|speedy|feu vert|point s|carglass|euromaster|"
    r"roady|carter[ -]?cash|oscaro|"
    # Immobilier
    r"century 21|orpi|lafor[eê]t|foncia|guy hoquet|era immobilier|"
    r"stéphane plaza|stephane plaza|iad\b|safti|capifrance|"
    r"nestenn|l'adresse|square habitat|arthurimmo|"
    # Optique / santé
    r"optical center|krys|afflelou|optic 2000|atol|"
    r"audika|amplifon|optical discount|générale d'optique|generale d.optique|"
    # Électronique / high-tech
    r"boulanger|darty|fnac|ldlc|but\b|conforama|"
    # Bricolage / maison
    r"leroy merlin|castorama|bricorama|bricomarch[eé]|mr\.? bricolage|"
    r"point\.?p|cedeo|gedimat|"
    # Sport
    r"decathlon|intersport|go sport|sport 2000|"
    # Hôtellerie
    r"ibis|novotel|mercure|premiere classe|premi[eè]re classe|"
    r"b&b hotel|kyriad|campanile|formule 1|f1 hotel|"
    r"holiday inn|best western|acc?or\b|hilton|marriott|"
    # Assurance / mutuelle
    r"groupama|maif|macif|axa|matmut|allianz|"
    r"harmonie mutuelle|ag2r|swisslife|swiss life|generali|"
    # Télécom
    r"bouygues|orange|sfr|free\b|"
    # Banque / finance
    r"soci[eé]t[eé] g[eé]n[eé]rale|bnp|cr[eé]dit agricole|"
    r"cr[eé]dit mutuel|caisse d.[eé]pargne|banque populaire|lcl|"
    r"la banque postale|hsbc|boursorama|"
    # Logistique / poste
    r"la poste|chronopost|dhl|ups\b|fedex|mondial relay|colissimo|"
    r"relais colis|gls\b|dpd\b|"
    # Services / divers
    r"manpower|adecco|randstad|interim|"
    r"century 21|samsic|onet|elior|sodexo|"
    r"veolia|suez|engie|edf\b|total[ é]nergie|totalenergie|"
    # Beauté / soins
    r"yves rocher|nocib[eé]|sephora|marionnaud|"
    r"jean[ -]?louis david|franck provost|tchip|coiff[&e]|"
    # Location / auto
    r"hertz|europcar|avis\b|sixt|enterprise|ada\b|"
    # Fitness
    r"basic[ -]?fit|keep cool|neoness|fitness park|l'orange bleue"
    r")\b",
    re.IGNORECASE,
)

# Mots génériques dans le nom qui signalent un groupe/chaîne
_GROUP_PATTERNS = re.compile(
    r"\b(franchise|group[e]?\b|holding|sa\b|sas\b|succursale|filiale)\b",
    re.IGNORECASE,
)


def _is_franchise(nom: str) -> bool:
    """Détecte si le nom ressemble à une franchise ou un groupe."""
    if _FRANCHISE_KEYWORDS.search(nom):
        return True
    if _GROUP_PATTERNS.search(nom):
        return True
    return False


def calculate_score(entreprise: dict) -> int:
    """
    Calcule un score de qualification pour un prospect.
    Priorité : accessibilité du décisionnaire > maturité digitale > taille.
    """
    score = 0
    nom = entreprise.get("nom", "")
    site = entreprise.get("site_web", "")
    emails = entreprise.get("emails", "")
    telephone = entreprise.get("telephone", "")
    note = _safe_float(entreprise.get("note", ""))
    nb_avis = entreprise.get("nb_avis", 0) or 0

    # --- Franchise / groupe → éliminé ---
    if _is_franchise(nom):
        return 0

    # Grosse structure probable
    if nb_avis >= 500:
        return 5
    if nb_avis >= 200:
        score -= 20
    elif nb_avis >= 100:
        score -= 10

    # ============================================
    # ACCESSIBILITÉ DU DÉCISIONNAIRE (max ~50 pts)
    # ============================================

    # Téléphone portable = ligne directe du patron
    if telephone and re.search(r"^0[67]", telephone):
        score += 25

    # Email = donnée exploitable pour automatisation
    if emails:
        prefix = emails.lower().split("@")[0] if "@" in emails else ""
        if prefix and prefix not in ("contact", "info", "accueil", "bonjour", "hello", "commercial", "devis"):
            score += 20  # Email nominatif = décideur accessible + donnée riche
        else:
            score += 15  # Email générique = point de contact automatisable

    # ============================================
    # MATURITÉ DIGITALE (max ~25 pts)
    # ============================================

    # Pas de site web = besoin digital fort
    if not site:
        score += 15
    else:
        if not emails:
            score += 10  # Site web sans email = présence faible

    # Note faible + assez d'avis = besoin d'automatisation relation client
    if note and note < 3.5 and nb_avis >= 20:
        score += 10

    # ============================================
    # TAILLE / PETITE STRUCTURE (max ~20 pts)
    # ============================================

    if nb_avis <= 5:
        score += 20  # Artisan / indépendant
    elif nb_avis <= 15:
        score += 15  # TPE
    elif nb_avis <= 50:
        score += 10  # Petite PME

    return max(0, min(score, 100))


def calculate_scores(entreprises: list[dict]) -> list[dict]:
    """Ajoute un score à chaque entreprise."""
    for e in entreprises:
        e["score"] = calculate_score(e)
    return entreprises


def score_label(score: int) -> str:
    """Retourne le label textuel du score."""
    if score >= 50:
        return "Hot"
    elif score >= 25:
        return "Warm"
    return "Cold"


def score_color(score: int) -> str:
    """Retourne la couleur CSS du score."""
    if score >= 50:
        return "#22c55e"
    elif score >= 25:
        return "#f59e0b"
    return "#94a3b8"


def _safe_float(val) -> float:
    try:
        return float(str(val).replace(",", "."))
    except (ValueError, TypeError):
        return 0.0
