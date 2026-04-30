"""Pipeline d'enrichissement avancé porté du bot France Travail.

Pour chaque entreprise (déjà scrapée + dirigeant trouvé en amont), enrichit avec :
1. Site web officiel (Perplexity Sonar + JSON Schema strict)
   - Validation lexicale + scrape de la page d'accueil
   - Fallback Google Maps si Perplexity n'a rien donné
2. Email du dirigeant (si prenom + nom connus)
   - Recherche ciblée Perplexity
   - Sinon : génération de patterns + validation SMTP RCPT TO
3. Emails stratégiques (direction@, rh@, dg@, gerance@)
   - Pré-check catchall pour éviter les faux positifs

Confiance des emails :
- "vérifié"  : SMTP RCPT TO confirmé par le serveur destinataire
- "probable" : domaine catchall, ou MX OK + pattern vraisemblable
- "incertain" : MX OK mais SMTP n'a pas pu trancher (OVH/IONOS unknown)

Cap de coûts : option `max_entreprises` (par défaut illimité, mais l'app
peut limiter pendant les tests pour ne pas brûler les crédits Perplexity).
"""

import logging
import re
import time
from urllib.parse import urlparse

import requests

import perplexity_search
import smtp_verifier
from validators import (
    HEADERS,
    extract_domain,
    extract_nom_variants,
    is_annuaire_domain,
    is_generic_email,
    is_parent_group_site,
    normalize_for_email,
    validate_site_matches_company,
)

logger = logging.getLogger(__name__)

# Préfixes stratégiques B2B (direction + RH), ordre = priorité
STRATEGIC_PREFIXES = [
    ("direction", "direction"),
    ("dg", "direction"),
    ("gerance", "direction"),
    ("directeur", "direction"),
    ("dir", "direction"),
    ("rh", "rh"),
    ("recrutement", "rh"),
    ("drh", "rh"),
]

# Timeouts user-spec
SITE_TIMEOUT = 5


def _normalize_site_url(site):
    """Force https:// + www. si absent. Retourne "" si vide."""
    if not site:
        return ""
    site = site.strip()
    if not site.startswith(("http://", "https://")):
        site = "https://" + site
    return site


def _generate_email_candidates(prenom_usuel, nom_variants, domain):
    """Patterns email FR triés par fréquence (porté du bot)."""
    initial = prenom_usuel[0] if prenom_usuel else ""
    if isinstance(nom_variants, str):
        nom_variants = [nom_variants]
    nom_variants = [n for n in nom_variants if n]
    if not nom_variants:
        return []
    primary = nom_variants[0]
    candidates = [
        prenom_usuel + "." + primary + "@" + domain,
        initial + "." + primary + "@" + domain,
        prenom_usuel + "@" + domain,
        primary + "@" + domain,
        prenom_usuel + "-" + primary + "@" + domain,
        initial + primary + "@" + domain,
        prenom_usuel + primary + "@" + domain,
        primary + "." + prenom_usuel + "@" + domain,
    ]
    for nom in nom_variants[1:]:
        candidates.extend([
            prenom_usuel + "." + nom + "@" + domain,
            initial + "." + nom + "@" + domain,
            nom + "@" + domain,
            prenom_usuel + "-" + nom + "@" + domain,
        ])
    return list(dict.fromkeys(candidates))


def find_dirigeant_email(prenom_brut, nom_brut, qualite, nom_entreprise,
                         site_web="", lieu=""):
    """Pipeline complet pour trouver l'email du dirigeant.

    1. Perplexity ciblée (si dispo)
    2. Génération patterns + SMTP RCPT TO

    Retourne (email, confiance) ou (None, None).
    confiance ∈ {"vérifié", "probable", "incertain"}
    """
    prenoms = (prenom_brut or "").strip().split()
    if not prenoms:
        return None, None
    prenom_usuel = normalize_for_email(prenoms[0])
    nom_variants = extract_nom_variants(nom_brut)
    if not prenom_usuel or not nom_variants:
        return None, None

    # --- Étape 1 : Perplexity ciblée ---
    if perplexity_search.is_available():
        try:
            pplx = perplexity_search.search_dirigeant(
                prenom_brut, nom_brut, qualite, nom_entreprise, site_web
            )
        except Exception as e:
            logger.debug("Perplexity dirigeant erreur : %s", e)
            pplx = None
        if pplx:
            email_found = pplx.get("email", "")
            if email_found and not is_generic_email(email_found):
                if smtp_verifier.is_available():
                    check = smtp_verifier.verify_email(email_found)
                    if check == "valid":
                        return email_found, "vérifié"
                    if check == "catchall":
                        return email_found, "probable"
                    if check == "unknown":
                        # OVH/IONOS : on garde l'email Perplexity en probable
                        return email_found, "probable"
                    # invalid / no_mx : Perplexity s'est trompé → patterns
                else:
                    return email_found, "probable"
            if not site_web and pplx.get("site_web"):
                site_web = pplx["site_web"]

    # --- Étape 2 : MX + génération patterns ---
    domain = extract_domain(site_web)
    if not domain:
        return None, None

    domain_status = "unknown"
    if smtp_verifier.is_available():
        domain_status = smtp_verifier.check_domain(domain)
        if domain_status == "no_mx":
            logger.info("MX check → %s : pas de MX", domain)
            return None, None

    candidates = _generate_email_candidates(prenom_usuel, nom_variants, domain)

    # Variante prénom complet (Marie-Claire, Jean-Pierre)
    prenom_complet = normalize_for_email(prenom_brut)
    if prenom_complet and prenom_complet != prenom_usuel:
        primary = nom_variants[0]
        candidates.extend([
            prenom_complet + "." + primary + "@" + domain,
            prenom_complet + "@" + domain,
            prenom_complet + "-" + primary + "@" + domain,
        ])
        candidates = list(dict.fromkeys(candidates))

    logger.info(
        "Email dirigeant → %d candidats pour %s %s @ %s (status=%s)",
        len(candidates), prenom_usuel, nom_variants[0], domain, domain_status,
    )

    # Domaine catchall : tout RCPT répondra accept → 1 candidat suffit (probable)
    if domain_status == "catchall":
        return candidates[0], "probable"

    # Pas de SMTP du tout : best guess non validé
    if not smtp_verifier.is_available():
        return candidates[0], "incertain"

    # On tente verify_email sur les top candidats. Cela couvre :
    # - domain_status == "ok" : verify donne des réponses fiables (valid/invalid)
    # - domain_status == "unknown" : le /catchall a échoué mais /verify peut marcher,
    #   et même s'il dit "unknown" c'est OVH/IONOS → on classe en "probable" (spec)
    limit = 6 if domain_status == "ok" else 4
    invalid_count = 0
    for email in candidates[:limit]:
        check = smtp_verifier.verify_email(email)
        if check == "valid":
            return email, "vérifié"
        if check == "catchall":
            return email, "probable"
        if check == "unknown":
            # Serveur MX accepte la connexion mais ne tranche pas (OVH/IONOS).
            # Spec : sans Debounce en fallback → "probable" sur le pattern principal.
            return candidates[0], "probable"
        if check == "invalid":
            invalid_count += 1
        if check in ("error", "no_mx"):
            # error = service HS, no_mx = pas d'email du tout
            break
        time.sleep(0.2)

    # Service "ok" + tous les candidats invalides → SMTP a tranché négatif
    if domain_status == "ok" and invalid_count >= 1:
        return None, None

    # Pas de signal SMTP exploitable (service flaky / timeout) → best guess
    return candidates[0], "incertain"


def find_strategic_emails(domain):
    """direction@, rh@, dg@, gerance@... validés via SMTP.
    Retourne liste de tuples (email, type, confiance).
    """
    if not domain or not smtp_verifier.is_available():
        return []

    # Pré-check catchall : si oui, garder direction@ uniquement (le reste
    # serait du faux positif)
    domain_status = smtp_verifier.check_domain(domain)
    if domain_status == "no_mx":
        return []
    if domain_status == "catchall":
        email = "direction@" + domain
        logger.info("Domaine catchall (%s) → %s en stratégique probable", domain, email)
        return [(email, "direction", "probable")]
    if domain_status == "unknown":
        # SMTP ne répond pas aux RCPT (OVH/IONOS notamment). Selon la spec,
        # sans Debounce en fallback on classe direction@ en probable.
        email = "direction@" + domain
        logger.info("Domaine unknown (%s) → %s en stratégique probable", domain, email)
        return [(email, "direction", "probable")]

    validated = []
    seen_types = set()
    consecutive_invalids = 0

    for prefix, email_type in STRATEGIC_PREFIXES:
        email = prefix + "@" + domain
        check = smtp_verifier.verify_email(email)

        if check == "valid":
            validated.append((email, email_type, "vérifié"))
            seen_types.add(email_type)
            consecutive_invalids = 0
        elif check == "catchall":
            # Domaine devenu catchall en cours de test
            if email_type == "direction" and "direction" not in seen_types:
                validated.append((email, email_type, "probable"))
                seen_types.add(email_type)
            break
        elif check == "invalid":
            consecutive_invalids += 1
            if consecutive_invalids >= 3:
                logger.debug("Stratégique : 3 invalids consécutifs sur %s, arrêt", domain)
                break
        elif check == "error":
            break

        time.sleep(0.2)

        if "direction" in seen_types and "rh" in seen_types:
            break

    return validated


def _scrape_site_emails(site_web, timeout=SITE_TIMEOUT, max_pages=4):
    """Scrape rapide pour récupérer les emails publiés sur le site officiel.
    Retourne un set d'emails (filet de sécurité quand Perplexity ne donne rien).
    """
    if not site_web:
        return set()
    site_web = _normalize_site_url(site_web)
    parsed = urlparse(site_web)
    base = parsed.scheme + "://" + parsed.netloc
    pages = [
        base, base + "/contact", base + "/mentions-legales",
        base + "/equipe", base + "/a-propos",
    ][:max_pages + 1]

    email_re = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
    found = set()
    for page in pages:
        try:
            resp = requests.get(page, timeout=timeout, headers=HEADERS, allow_redirects=True)
            if resp.status_code != 200:
                continue
            for e in email_re.findall(resp.text):
                e = e.strip().rstrip(".")
                if e.lower().endswith((".png", ".jpg", ".gif", ".svg", ".css", ".js")):
                    continue
                if any(x in e.lower() for x in ["noreply", "no-reply", "example.com", "wixpress"]):
                    continue
                found.add(e)
            if found:
                break
        except Exception:
            continue
    return found


def enrich_one(entreprise, do_perplexity=True, do_strategic=True):
    """Enrichit une seule entreprise. Modifie le dict en place et le retourne.

    Champs ajoutés / mis à jour :
    - site_web (validé ou rejeté)
    - email_dirigeant (si prenom+nom connus + email trouvé)
    - email_dirigeant_confiance ∈ {"vérifié", "probable", "incertain", ""}
    - emails_strategiques : liste de (email, type, confiance)
    - enrichment_log : str (étapes franchies, pour debug)

    Toutes les étapes externes sont entourées de try/except : un échec ne
    fait jamais crasher l'enrichissement, on log et on passe.
    """
    log_parts = []
    nom = (entreprise.get("nom") or "").strip()
    if not nom:
        entreprise["enrichment_log"] = "skip: pas de nom"
        return entreprise

    # Construire un "lieu" pour Perplexity / GMaps (adresse ou ville)
    adresse = entreprise.get("adresse", "") or ""
    ville = entreprise.get("ville", "") or ""
    lieu = adresse or ville
    siren = entreprise.get("siren", "") or ""

    # --- 1. Site web : valider l'existant ou chercher via Perplexity ---
    site_web = (entreprise.get("site_web") or "").strip()
    site_web_was_valid = False

    if site_web:
        if is_annuaire_domain(site_web):
            log_parts.append("site rejeté (annuaire): " + site_web)
            site_web = ""
        else:
            try:
                if validate_site_matches_company(site_web, nom, timeout=SITE_TIMEOUT):
                    site_web_was_valid = True
                    log_parts.append("site existant validé")
                else:
                    log_parts.append("site existant rejeté (validation)")
                    site_web = ""
            except Exception as e:
                logger.debug("Validation site erreur: %s", e)
                log_parts.append("site existant non validable (erreur)")

    # Recherche Perplexity si pas de site valide
    pplx_emails = set()
    if not site_web and do_perplexity and perplexity_search.is_available():
        try:
            pplx = perplexity_search.search_company(nom, lieu, siren)
        except Exception as e:
            logger.debug("Perplexity company erreur: %s", e)
            pplx = None
        if pplx:
            if pplx.get("site_web"):
                site_web = pplx["site_web"]
                log_parts.append("site Perplexity: " + site_web)
            for e in pplx.get("emails", []):
                if e and "@" in e:
                    pplx_emails.add(e)
        else:
            log_parts.append("perplexity company: rien")

    # Fallback GMaps si toujours pas de site
    if not site_web:
        try:
            from gmaps_lookup import find_company_site
            gmaps_site = find_company_site(nom, lieu)
        except Exception as e:
            logger.debug("GMaps lookup erreur: %s", e)
            gmaps_site = ""
        if gmaps_site:
            site_web = gmaps_site
            log_parts.append("site GMaps: " + site_web)

    if site_web:
        entreprise["site_web"] = site_web

    # Stocker les emails Perplexity découverts pour fallback
    if pplx_emails:
        existing = entreprise.get("emails", "") or ""
        # On garde "emails" (singulier) pour la rétro-compat de l'app, mais
        # ajoute pplx_emails si vide
        if not existing:
            # Sélection : préférer un email perso non-générique si possible
            non_gen = [e for e in pplx_emails if not is_generic_email(e)]
            entreprise["emails"] = non_gen[0] if non_gen else next(iter(pplx_emails))

    # --- 2. Email dirigeant ---
    prenom = (entreprise.get("dirigeant_prenom") or "").strip()
    nom_dir = (entreprise.get("dirigeant_nom") or "").strip()
    qualite = (entreprise.get("dirigeant_qualite") or "").strip()

    email_dir = entreprise.get("email_dirigeant", "") or ""
    email_dir_conf = entreprise.get("email_dirigeant_confiance", "") or ""

    # Mapper l'ancien email_dirigeant_confidence (high/medium/low) si présent
    if not email_dir_conf and entreprise.get("email_dirigeant_confidence"):
        old = entreprise["email_dirigeant_confidence"]
        email_dir_conf = {
            "high": "vérifié", "medium": "probable", "low": "incertain",
        }.get(old, "")

    if prenom and nom_dir:
        if not email_dir:
            try:
                e, conf = find_dirigeant_email(prenom, nom_dir, qualite, nom, site_web, lieu)
            except Exception as exc:
                logger.debug("find_dirigeant_email erreur: %s", exc)
                e, conf = None, None
            if e:
                email_dir = e
                email_dir_conf = conf or ""
                log_parts.append("email dirigeant: " + e + " (" + email_dir_conf + ")")
            else:
                log_parts.append("email dirigeant: introuvable")
        elif not email_dir_conf and smtp_verifier.is_available():
            # On a un email mais pas de confiance : valider
            try:
                check = smtp_verifier.verify_email(email_dir)
            except Exception:
                check = "error"
            if check == "valid":
                email_dir_conf = "vérifié"
            elif check in ("catchall", "unknown"):
                email_dir_conf = "probable"
            elif check in ("invalid", "no_mx"):
                email_dir = ""
                email_dir_conf = ""

    entreprise["email_dirigeant"] = email_dir
    entreprise["email_dirigeant_confiance"] = email_dir_conf

    # --- 3. Emails stratégiques ---
    strategic = []
    if do_strategic and site_web and smtp_verifier.is_available():
        domain = extract_domain(site_web)
        if domain:
            try:
                strategic = find_strategic_emails(domain)
            except Exception as e:
                logger.debug("find_strategic_emails erreur: %s", e)
                strategic = []
            if strategic:
                log_parts.append(str(len(strategic)) + " email(s) stratégique(s)")
    entreprise["emails_strategiques"] = strategic

    # --- 4. Validation de l'email scrapé existant (si pas de confiance) ---
    scraped_email = (entreprise.get("emails", "") or "").strip()
    scraped_conf = entreprise.get("emails_confiance", "") or ""
    # Si on a un email_status existant, le mapper
    if not scraped_conf and entreprise.get("email_status"):
        scraped_conf = {
            "valid": "vérifié", "catchall": "probable",
        }.get(entreprise["email_status"], "")
    if scraped_email and not scraped_conf and smtp_verifier.is_available():
        if not is_generic_email(scraped_email):
            try:
                check = smtp_verifier.verify_email(scraped_email)
            except Exception:
                check = "error"
            if check == "valid":
                scraped_conf = "vérifié"
            elif check == "catchall":
                scraped_conf = "probable"
            elif check == "unknown":
                # SMTP ne tranche pas (OVH/IONOS) → probable selon la spec
                scraped_conf = "probable"
            elif check in ("invalid", "no_mx"):
                # Drop l'email scrapé si invalide
                entreprise["emails"] = ""
                scraped_email = ""
            # check == "error" : laisser sans confidence (l'email reste)
        else:
            # Email générique trouvé sur le site : on garde mais en "probable"
            # (peu de signal de prospection mais il existe sur le site officiel)
            scraped_conf = "probable"
    entreprise["emails_confiance"] = scraped_conf

    entreprise["enrichment_log"] = " | ".join(log_parts) if log_parts else ""
    logger.info(
        "Enrich %s : email_dir=%s (%s), strat=%d, site=%s",
        nom, email_dir or "-", email_dir_conf or "-",
        len(strategic),
        site_web or "-",
    )
    return entreprise


def enrich_advanced(entreprises, progress_callback=None,
                    do_perplexity=True, do_strategic=True,
                    max_entreprises=None):
    """Enrichissement avancé sur une liste d'entreprises.

    do_perplexity : appelle Perplexity Sonar (sinon : juste validation + GMaps + SMTP)
    do_strategic  : recherche direction@, rh@, etc.
    max_entreprises : cap pour éviter de brûler les crédits pendant les tests
    """
    smtp_verifier.reset_cache()
    total = len(entreprises) if max_entreprises is None else min(len(entreprises), max_entreprises)

    if progress_callback:
        progress_callback("Enrichissement avancé sur %d entreprises..." % total, 0.0)

    for idx, ent in enumerate(entreprises):
        if max_entreprises is not None and idx >= max_entreprises:
            # Marquer les autres comme non-enrichies
            ent.setdefault("email_dirigeant_confiance", "")
            ent.setdefault("emails_strategiques", [])
            ent.setdefault("emails_confiance", "")
            continue
        if progress_callback:
            progress_callback(
                "Enrichissement %d/%d : %s" % (idx + 1, total, ent.get("nom", "")),
                (idx + 1) / max(total, 1),
            )
        try:
            enrich_one(ent, do_perplexity=do_perplexity, do_strategic=do_strategic)
        except Exception as e:
            logger.warning("Enrich one a crashé pour %s : %s", ent.get("nom", "?"), e)
            ent.setdefault("email_dirigeant_confiance", "")
            ent.setdefault("emails_strategiques", [])
            ent.setdefault("emails_confiance", "")

    if progress_callback:
        progress_callback("Enrichissement avancé terminé !", 1.0)
    return entreprises
