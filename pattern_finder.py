"""Génération de patterns email + validation SMTP — RÈGLE STRICTE.

Cette logique ne tourne **JAMAIS** sur un MX opaque (OVH, IONOS, CleanMail,
les serveurs qui répondent `unknown` à tout RCPT TO). Un pattern ne peut
être conservé que s'il revient `valid` net du SMTP RCPT TO — c'est cette
exigence qui maximise le taux non-bounce dans Debounce.

Deux usages :

- `find_dirigeant_email(...)` : pour une entreprise où l'on connaît le
  dirigeant (prénom + nom) et où aucun email publié n'a été scrapé. On teste
  la liste complète des patterns (~15 candidats), un par un, jusqu'à trouver
  un `valid` ou épuiser la liste.

- `find_generic_email(...)` : tente `contact@` / `direction@` / `info@`. On
  ne garde que ceux qui reviennent `valid`. Sert quand pas de dirigeant ou
  quand son email n'existe pas.

Aucun fallback vers "incertain"/"probable" : si SMTP ne dit pas `valid`,
on ne propose rien.
"""

import logging
import time

import smtp_verifier
from validators import extract_nom_variants, normalize_for_email


logger = logging.getLogger(__name__)


def generate_dirigeant_patterns(prenom_brut, nom_brut, domain):
    """Liste complète des patterns plausibles (FR B2B), ordre = priorité.
    Retourne une liste dédupliquée.
    """
    if not domain:
        return []
    prenoms = (prenom_brut or "").strip().split()
    if not prenoms:
        return []
    prenom_usuel = normalize_for_email(prenoms[0])
    prenom_complet = normalize_for_email(prenom_brut)
    nom_variants = extract_nom_variants(nom_brut)
    if not prenom_usuel or not nom_variants:
        return []
    initial = prenom_usuel[0] if prenom_usuel else ""

    candidates = []
    for nom in nom_variants:
        candidates.extend([
            prenom_usuel + "." + nom + "@" + domain,
            initial + "." + nom + "@" + domain,
            initial + nom + "@" + domain,
            prenom_usuel + nom + "@" + domain,
            prenom_usuel + "-" + nom + "@" + domain,
            prenom_usuel + "_" + nom + "@" + domain,
            nom + "." + prenom_usuel + "@" + domain,
            nom + prenom_usuel + "@" + domain,
        ])
    # Prénom seul / nom seul
    candidates.append(prenom_usuel + "@" + domain)
    for nom in nom_variants:
        candidates.append(nom + "@" + domain)

    # Variantes prénom complet (Marie-Claire, Jean-Pierre)
    if prenom_complet and prenom_complet != prenom_usuel:
        primary = nom_variants[0]
        candidates.extend([
            prenom_complet + "." + primary + "@" + domain,
            prenom_complet + primary + "@" + domain,
            prenom_complet + "-" + primary + "@" + domain,
            prenom_complet + "@" + domain,
        ])

    # Dédup en gardant l'ordre
    return list(dict.fromkeys(candidates))


def find_dirigeant_email(prenom, nom, domain, source_url=""):
    """Cherche l'email du dirigeant sur un MX discriminatif.

    Politique : on teste tous les patterns dans l'ordre. On garde uniquement
    le premier qui revient `valid`. Si aucun n'est `valid`, on ne propose RIEN.

    L'appelant doit avoir confirmé en amont que `mx_type == 'discriminatif'`
    — ce module ne re-check pas.

    Retourne un dict email (forme `entreprise_emails`) ou None.
    """
    candidates = generate_dirigeant_patterns(prenom, nom, domain)
    if not candidates:
        return None

    logger.info(
        "Pattern dirigeant → %d candidats sur %s pour %s %s",
        len(candidates), domain, prenom, nom,
    )

    error_count = 0
    for email in candidates:
        status = smtp_verifier.verify_email(email)
        if status == "valid":
            logger.info("Pattern → %s VALID", email)
            return {
                "email": email,
                "source": "pattern",
                "source_url": source_url,
                "smtp_status": "valid",
                "is_public_domain": False,
                "destinataire_rank": 1,
            }
        if status == "no_mx":
            return None  # pas de MX sur ce domaine, inutile de continuer
        if status == "error":
            error_count += 1
            if error_count >= 3:
                break
        time.sleep(0.15)  # courtoisie vis-à-vis du MX cible

    logger.info("Pattern dirigeant → aucun valid trouvé sur %s", domain)
    return None


_GENERIC_PREFIXES = [
    ("direction", 2), ("dg", 2), ("gerance", 2), ("directeur", 2),
    ("contact", 4), ("info", 4), ("accueil", 4), ("hello", 4),
]


def find_generic_email(domain, source_url=""):
    """Cherche un email générique (direction@ / contact@ / ...) SMTP valid.

    L'appelant doit avoir confirmé en amont que `mx_type == 'discriminatif'`.
    Retourne un dict email ou None.
    """
    if not domain:
        return None
    error_count = 0
    for prefix, rank in _GENERIC_PREFIXES:
        email = prefix + "@" + domain
        status = smtp_verifier.verify_email(email)
        if status == "valid":
            logger.info("Generic → %s VALID", email)
            return {
                "email": email,
                "source": "generic",
                "source_url": source_url,
                "smtp_status": "valid",
                "is_public_domain": False,
                "destinataire_rank": rank,
            }
        if status == "no_mx":
            return None
        if status == "error":
            error_count += 1
            if error_count >= 3:
                break
        time.sleep(0.15)
    return None
