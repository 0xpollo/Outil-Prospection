"""Scoring des prospects calibré sur la qualité du meilleur email disponible."""

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


# Source de l'email triée du plus direct au plus générique
_DIRIGEANT_SOURCES = {"dirigeant"}
_DIRECTION_SOURCES = {"direction", "rh", "metier"}
_GENERIQUE_SOURCES = {"site", "contact"}
# "perso" (gmail/outlook/…) traité à part : MX non testable -> Froid systématique

# Liste unique des domaines perso : importée depuis email_finder pour éviter
# la duplication. On garde un alias `_PUBLIC_DOMAINS` pour le code historique.
from email_finder import PUBLIC_EMAIL_DOMAINS as _PUBLIC_DOMAINS

# Mapping legacy DB (high/medium/low) -> labels FR
_CONF_LEGACY = {"high": "vérifié", "medium": "probable", "low": "incertain"}
# Mapping email_status SMTP -> confiance
_STATUS_TO_CONF = {
    "valid": "vérifié",
    "catchall": "probable",
    "public": "probable",
    "unknown": "incertain",
}


def _best_email_signals(e):
    """Renvoie (source, confiance) du meilleur email disponible, ou None.

    Mêmes règles que app.pick_best_email mais sans dépendance Streamlit :
    on classe par confiance d'abord, puis par directness de la source.
    """
    candidates = []

    if (e.get("email_dirigeant") or "").strip():
        c = (e.get("email_dirigeant_confiance")
             or e.get("email_dirigeant_confidence") or "")
        c = _CONF_LEGACY.get(c, c) or ""
        candidates.append(("dirigeant", c))

    email_s = (e.get("emails") or "").strip()
    if email_s:
        domain = email_s.split("@", 1)[1].lower() if "@" in email_s else ""
        is_public = (e.get("email_status") == "public") or (domain in _PUBLIC_DOMAINS)
        if is_public:
            candidates.append(("perso", "incertain"))
        else:
            c = e.get("emails_confiance") or ""
            if not c:
                c = _STATUS_TO_CONF.get(e.get("email_status") or "", "")
            candidates.append(("site", c))

    # emails_strategiques peut arriver comme list (in-memory) ou string JSON
    # (lecture brute DB par un script). On accepte les deux.
    strat = e.get("emails_strategiques") or []
    if isinstance(strat, str):
        try:
            import json as _json
            strat = _json.loads(strat) if strat else []
        except (ValueError, TypeError):
            strat = []
    for tup in strat:
        if isinstance(tup, (list, tuple)) and len(tup) >= 2 and tup[0]:
            typ = tup[1] or "contact"
            conf = tup[2] if len(tup) >= 3 else ""
            candidates.append((typ, conf or "probable"))

    if not candidates:
        return None

    conf_rank = {"vérifié": 0, "probable": 1, "incertain": 2, "": 3}
    src_rank = {"dirigeant": 0, "direction": 1, "rh": 1, "metier": 1,
                "site": 2, "contact": 2, "perso": 3}
    candidates.sort(key=lambda c: (conf_rank.get(c[1], 4),
                                   src_rank.get(c[0], 9)))
    return candidates[0]


def calculate_score(entreprise):
    """Score 0-100 calibré uniquement sur la qualité du meilleur email.

    Grille (cf. CLAUDE.md) :
      90  A Hot   - nominatif dirigeant + vérifié SMTP
      70  B Chaud - (dirigeant + probable) OU (direction/RH + vérifié)
      40  C Tiède - dirigeant incertain/non testé, ou direction probable,
                     ou générique vérifié
      10  D Froid - générique probable/incertain, ou aucun email
       0  Exclu  - franchise / groupe / chaîne
    """
    if _is_franchise(entreprise.get("nom", "")):
        return 0

    sig = _best_email_signals(entreprise)
    if sig is None:
        return 10

    source, conf = sig
    is_dir = source in _DIRIGEANT_SOURCES
    is_mid = source in _DIRECTION_SOURCES
    is_gen = source in _GENERIQUE_SOURCES

    if is_dir and conf == "vérifié":
        return 90
    if (is_dir and conf == "probable") or (is_mid and conf == "vérifié"):
        return 70
    if is_dir:  # incertain ou non testé ("")
        return 40
    if is_mid and conf in ("probable", "incertain"):
        return 40
    if is_gen and conf == "vérifié":
        return 40
    return 10


def calculate_scores(entreprises):
    """Ajoute un score à chaque entreprise."""
    for e in entreprises:
        e["score"] = calculate_score(e)
    return entreprises


def score_label(score):
    """Label texte du score (cohérent avec la grille A/B/C/D)."""
    if score >= 90:
        return "Hot"
    if score >= 70:
        return "Chaud"
    if score >= 40:
        return "Tiède"
    if score > 0:
        return "Froid"
    return "Exclu"


def score_color(score):
    """Couleur CSS du badge de score."""
    if score >= 90:
        return "#16a34a"   # vert
    if score >= 70:
        return "#84cc16"   # vert clair
    if score >= 40:
        return "#f59e0b"   # orange
    if score > 0:
        return "#94a3b8"   # gris (Froid)
    return "#64748b"       # gris foncé (Exclu)


