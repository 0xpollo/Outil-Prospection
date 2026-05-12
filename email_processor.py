"""Logique pure sur la liste d'emails d'une entreprise.

Une entreprise expose en mémoire `entreprise["emails"]` : list[dict] avec
clés `email, source, source_url, smtp_status, is_public_domain,
destinataire_rank`. Ce module fournit :

- détection des domaines publics (gmail/outlook/...)
- assignation du rang destinataire (1..7) selon le préfixe + contexte
- choix du meilleur email pour la prospection (validité × rang)
- classification de l'entreprise en tier P0 / P1 / P2 / X

Aucun appel réseau ici. Tout est déterministe sur les données déjà
collectées par le pipeline.
"""

import re
import unicodedata

from validators import extract_domain, extract_nom_variants, normalize_for_email


# Domaines de mail grand public (l'email a été publié par l'utilisateur sur
# son site mais ne mène pas à un domaine d'entreprise). Toujours non-vérifiable
# côté SMTP (MX accept-all).
PUBLIC_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.fr",
    "hotmail.com", "hotmail.fr", "outlook.com", "outlook.fr",
    "live.com", "live.fr", "msn.com", "orange.fr", "wanadoo.fr",
    "free.fr", "laposte.net", "sfr.fr", "bbox.fr", "numericable.fr",
    "icloud.com", "me.com", "mac.com", "aol.com", "aol.fr",
    "protonmail.com", "proton.me", "gmx.fr", "gmx.com",
}


# Préfixes locaux -> rang destinataire de base (1, 2, 4, 6, 7). Les rangs 3 et 5
# sont réservés aux emails sur domaine public (assignés en fonction du contexte
# entreprise dans assign_destinataire_ranks).
_PREFIX_RANK = {
    # 2 — direction
    "direction": 2, "dg": 2, "gerance": 2, "gérance": 2, "directeur": 2,
    "dir": 2, "directrice": 2, "president": 2, "président": 2, "pdg": 2,
    "patron": 2, "patronne": 2, "gerant": 2, "gérant": 2,
    # 4 — contact générique
    "contact": 4, "info": 4, "infos": 4, "accueil": 4, "hello": 4,
    "bonjour": 4, "coucou": 4, "salut": 4,
    # 6 — commercial
    "commercial": 6, "commerciale": 6, "commerciaux": 6, "devis": 6,
    "vente": 6, "ventes": 6, "sales": 6,
    # 7 — RH
    "rh": 7, "recrutement": 7, "drh": 7, "hr": 7, "jobs": 7, "carriere": 7,
    "carrieres": 7, "emploi": 7, "emplois": 7,
}


def is_public_email(email):
    """True si l'email est sur un domaine grand public (gmail/outlook/...)."""
    if not email or "@" not in email:
        return False
    domain = email.split("@", 1)[1].lower().strip()
    return domain in PUBLIC_EMAIL_DOMAINS


def email_domain(email):
    """Retourne le domaine de l'email, ou '' si invalide."""
    if not email or "@" not in email:
        return ""
    return email.split("@", 1)[1].lower().strip()


def _strip_accents(text):
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _looks_like_dirigeant_local(local, prenom_norm, nom_variants):
    """True si le local-part ressemble à un email nominatif du dirigeant.
    Couvre les patterns courants : prenom.nom, p.nom, pnom, prenom-nom,
    prenomnom, nom.prenom, prenom, nom."""
    if not local or (not prenom_norm and not nom_variants):
        return False
    local = local.lower()
    p = prenom_norm
    initial = p[0] if p else ""
    for n in nom_variants:
        if not n:
            continue
        candidates = set()
        if p:
            candidates.update({
                p + "." + n, p + n, p + "-" + n, p + "_" + n,
                n + "." + p, n + p, n + "-" + p,
            })
        if initial:
            candidates.update({initial + "." + n, initial + n, initial + "-" + n})
        candidates.add(n)
        if local in candidates:
            return True
    if p and (local == p or local.startswith(p + ".") or local.startswith(p + "-")):
        return True
    return False


def assign_destinataire_ranks(entreprise):
    """Met à jour le `destinataire_rank` de chaque email de l'entreprise.

    Règles :
      1 — dirigeant nominatif (matche prénom/nom du dirigeant connu)
      2 — direction / dg / gerance / directeur
      3 — perso (gmail/...) ET l'entreprise n'a aucun autre email pro
      4 — contact / info / accueil / hello / bonjour (générique)
      5 — perso ET l'entreprise a au moins un autre email @sondomaine
      6 — commercial / devis
      7 — rh / recrutement / drh
    Inconnu → 4 (générique contact par défaut, neutre).
    """
    emails = entreprise.get("emails") or []
    if not emails:
        return

    prenom_norm = normalize_for_email(
        (entreprise.get("dirigeant_prenom") or "").split()[0]
        if entreprise.get("dirigeant_prenom") else ""
    )
    nom_variants = extract_nom_variants(entreprise.get("dirigeant_nom") or "")

    # Y a-t-il au moins un email pro (non-public) dans la liste ?
    has_corporate = any(not e.get("is_public_domain") for e in emails)

    for e in emails:
        email = (e.get("email") or "").lower()
        if "@" not in email:
            e["destinataire_rank"] = 4
            continue
        local = email.split("@", 1)[0]
        # Cas perso : rang 3 (solo) ou 5 (a aussi du pro)
        if e.get("is_public_domain"):
            e["destinataire_rank"] = 5 if has_corporate else 3
            continue
        # Dirigeant nominatif
        if _looks_like_dirigeant_local(local, prenom_norm, nom_variants):
            e["destinataire_rank"] = 1
            continue
        # Préfixe générique connu (sans tenir compte des séparateurs)
        base = re.split(r"[.\-_]", local, 1)[0]
        rank = _PREFIX_RANK.get(local) or _PREFIX_RANK.get(base)
        if rank:
            e["destinataire_rank"] = rank
            continue
        e["destinataire_rank"] = 4


# Validité ordonnée pour le tri : plus petit = mieux
_VALIDITY_RANK = {
    "valid": 0,
    "catchall": 1,
    "unknown": 2,
    "": 2,        # pas testé = traité comme unknown
    "error": 2,
    "public": 2,  # MX accept-all (gmail/...), traité comme unknown
}


def email_sort_key(item):
    """Clé de tri d'un email : (validité, rang destinataire).
    Le plus petit gagne."""
    status = (item.get("smtp_status") or "").lower()
    return (
        _VALIDITY_RANK.get(status, 3),
        int(item.get("destinataire_rank") or 4),
    )


def pick_best_email(entreprise):
    """Retourne le meilleur email d'une entreprise (dict) ou None.

    Tri = (validité, rang destinataire). Validité prime :
    un contact@valid bat un dirigeant@catchall.
    """
    emails = entreprise.get("emails") or []
    if not emails:
        return None
    return sorted(emails, key=email_sort_key)[0]


# ---------------------------------------------------------------------------
# Tier classification (P0 / P1 / P2 / X)
# ---------------------------------------------------------------------------

# Mots dans le nom qui signalent une franchise / groupe / chaîne (exclu).
_FRANCHISE_KEYWORDS = re.compile(
    r"\b("
    r"mcdonald|burger king|kfc|subway|domino|pizza hut|starbucks|"
    r"five guys|quick|o'tacos|otacos|hippopotamus|buffalo grill|"
    r"flunch|courtepaille|del arte|popeyes|paul\b|class'croute|"
    r"brioche dor[ée]e|marie blach[eè]re|exki|cojean|"
    r"carrefour|leclerc|auchan|lidl|intermarch[eé]|casino|monoprix|"
    r"franprix|picard|biocoop|grand frais|netto|aldi|cora|"
    r"super u\b|hyper u\b|march[eé] u\b|syst[eè]me u|"
    r"norauto|midas|speedy|feu vert|point s|carglass|euromaster|"
    r"roady|"
    r"century 21|orpi|lafor[eê]t|foncia|guy hoquet|era immobilier|"
    r"st[ée]phane plaza|iad\b|safti|capifrance|nestenn|"
    r"optical center|krys|afflelou|optic 2000|atol|audika|amplifon|"
    r"boulanger|darty|fnac|ldlc|but\b|conforama|"
    r"leroy merlin|castorama|bricorama|bricomarch[eé]|mr\.? bricolage|"
    r"point\.?p|cedeo|gedimat|"
    r"decathlon|intersport|go sport|sport 2000|"
    r"ibis|novotel|mercure|premi[èe]re classe|b&b hotel|kyriad|"
    r"campanile|formule 1|f1 hotel|holiday inn|best western|"
    r"acc?or\b|hilton|marriott|"
    r"groupama|maif|macif|axa|matmut|allianz|harmonie mutuelle|"
    r"ag2r|swisslife|swiss life|generali|"
    r"bouygues|orange|sfr|free\b|"
    r"soci[eé]t[eé] g[eé]n[eé]rale|bnp|cr[eé]dit agricole|"
    r"cr[eé]dit mutuel|caisse d.[eé]pargne|banque populaire|lcl|"
    r"la banque postale|hsbc|boursorama|"
    r"la poste|chronopost|dhl|ups\b|fedex|mondial relay|colissimo|"
    r"manpower|adecco|randstad|"
    r"veolia|suez|engie|edf\b|total[ é]nergie|totalenergie|"
    r"yves rocher|nocib[eé]|sephora|marionnaud|"
    r"jean[ -]?louis david|franck provost|tchip|"
    r"hertz|europcar|avis\b|sixt|enterprise|ada\b|"
    r"basic[ -]?fit|keep cool|neoness|fitness park|l'orange bleue"
    r")\b",
    re.IGNORECASE,
)


def is_franchise(nom):
    """True si le nom de l'entreprise ressemble à une franchise/chaîne connue."""
    if not nom:
        return False
    return bool(_FRANCHISE_KEYWORDS.search(nom))


def classify_tier(entreprise):
    """Tier final de l'entreprise pour l'export.

    P0 — best email SMTP `valid` (n'importe quel rang)
       OU best email `published` avec smtp `catchall`
    P1 — best email `published` (corporate) avec smtp `unknown`/``
       OU best email sur domaine public (gmail/...) scrapé sur le site officiel
    P2 — dirigeant connu mais aucun email exploitable
       OU pas de dirigeant mais site/téléphone disponible
    X  — franchise/chaîne détectée (exclu)
    """
    if is_franchise(entreprise.get("nom") or ""):
        return "X"

    best = pick_best_email(entreprise)
    if best is None:
        return "P2"

    status = (best.get("smtp_status") or "").lower()
    source = (best.get("source") or "").lower()
    is_public = bool(best.get("is_public_domain"))

    if status == "valid":
        return "P0"
    if source == "published" and status == "catchall":
        return "P0"
    if source == "published" and not is_public:
        # unknown / "" sur corporate publié → P1 (publié = signal fort)
        return "P1"
    if source == "published" and is_public:
        # gmail/outlook scrapé sur site officiel → P1
        return "P1"
    # source pattern / generic non valid → P2 (jamais en P0/P1 sans valid)
    return "P2"


def compute_tier_and_rank_for_all(entreprises):
    """Helper pratique : assigne ranks puis tier sur toute la liste."""
    for e in entreprises:
        assign_destinataire_ranks(e)
        e["tier"] = classify_tier(e)


# ---------------------------------------------------------------------------
# Outils annexes pour l'UI
# ---------------------------------------------------------------------------

_TIER_LABEL = {
    "P0": "P0 — Envoi",
    "P1": "P1 — Envoi",
    "P2": "P2 — Manuel",
    "X": "Exclu",
}

_TIER_COLOR = {
    "P0": "#16a34a",   # vert
    "P1": "#84cc16",   # vert clair
    "P2": "#f59e0b",   # orange
    "X": "#64748b",    # gris foncé
}


def tier_label(tier):
    return _TIER_LABEL.get(tier or "", tier or "")


def tier_color(tier):
    return _TIER_COLOR.get(tier or "", "#94a3b8")
