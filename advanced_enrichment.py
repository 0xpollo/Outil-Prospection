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

# Préfixes stratégiques B2B, ordre = priorité de prospection.
# Tier 1 : direction (décideur)
# Tier 2 : RH (interlocuteur du besoin)
# Tier 3 : contact générique — moins prioritaire mais souvent le seul email
#         existant sur les TPE artisanales
STRATEGIC_PREFIXES = [
    ("direction", "direction"),
    ("dg", "direction"),
    ("gerance", "direction"),
    ("directeur", "direction"),
    ("dir", "direction"),
    ("rh", "rh"),
    ("recrutement", "rh"),
    ("drh", "rh"),
    ("contact", "contact"),
    ("info", "contact"),
    ("accueil", "contact"),
    ("hello", "contact"),
    ("commercial", "contact"),
    # Préfixes "métier" (cabinets, études, agences). Souvent le seul email
    # standard qui existe sur les domaines de pro libéral / TPE B2B.
    ("cabinet", "metier"),
    ("etude", "metier"),
    ("agence", "metier"),
    ("bureau", "metier"),
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


# Mots métiers / activités trop génériques pour servir de slug seul.
# Empêche "LUXE GARAGE" → garage.com (qui existe mais n'est pas l'entreprise).
_METIER_STOP = {
    "garage", "garages", "plombier", "plomberie", "plombiers",
    "restaurant", "restaurants", "boulangerie", "boulanger",
    "coiffure", "coiffeur", "menuiserie", "menuisier", "menuiseries",
    "electricien", "electricite", "electriciens", "carrelage",
    "peinture", "peintre", "macon", "maconnerie", "couverture",
    "couvreur", "chauffage", "chauffagiste", "vitrerie",
    "automobile", "automobiles", "auto", "autos", "transport",
    "concession", "transports", "logistique", "distribution",
    "service", "services", "solutions", "expert", "comptable",
    "comptables", "cabinet", "agence", "atelier",
}


def _guess_domain_candidates(nom_entreprise):
    """Slugs plausibles à tester comme domaines : avec tirets, sans tirets.
    On ne renvoie PAS un mot seul (trop souvent un mot métier qui matche un
    domaine sans rapport — cf. LUXE GARAGE → garage.com).

    Ex: "LUXE GARAGE" → ['luxegarage', 'luxe-garage']
        "GARAGE MEKKI" → ['garagemekki', 'garage-mekki']
    """
    if not nom_entreprise:
        return []
    import re as _re
    import unicodedata as _ud
    no_accents = _ud.normalize("NFD", nom_entreprise.lower())
    no_accents = "".join(c for c in no_accents if _ud.category(c) != "Mn")
    cleaned = _re.sub(r"[^a-z0-9\s]", " ", no_accents).strip()
    # Retirer formes juridiques + petits mots
    for stop in ["sarl", "sas", "sasu", "eurl", "sci", "ste", "scop",
                 "societe", "groupe", "ets", "earl", "sa", "snc",
                 "et", "de", "du", "des", "la", "le", "les"]:
        cleaned = _re.sub(r"\b" + stop + r"\b", " ", cleaned)
    tokens = [t for t in cleaned.split() if t and len(t) >= 2]
    if not tokens:
        return []

    # Si tous les tokens sont des mots métiers génériques, on abandonne :
    # impossible de deviner un domaine d'entreprise spécifique.
    distinctive = [t for t in tokens if t not in _METIER_STOP]
    if not distinctive:
        return []

    candidates = []
    # Slug joint (luxegarage / garagemekki)
    joined = "".join(tokens)
    if 5 <= len(joined) <= 30:
        candidates.append(joined)
    # Slug avec tirets (luxe-garage)
    hyphenated = "-".join(tokens)
    if hyphenated != joined and 5 <= len(hyphenated) <= 35:
        candidates.append(hyphenated)
    # Slug des seuls tokens distinctifs si différent (ex: "GARAGE MEKKI" → "mekki"
    # est rejeté ci-dessus, mais "GARAGE LUXE PARIS" → distinctifs ['luxe','paris']
    # → 'luxeparis' / 'luxe-paris' à tester)
    if len(distinctive) >= 2:
        d_joined = "".join(distinctive)
        d_hyph = "-".join(distinctive)
        if d_joined != joined and 5 <= len(d_joined) <= 30 and d_joined not in candidates:
            candidates.append(d_joined)
        if d_hyph != hyphenated and 5 <= len(d_hyph) <= 35 and d_hyph not in candidates:
            candidates.append(d_hyph)
    return candidates[:3]  # cap 3 pour limiter le coût SMTP


def try_guess_emails_from_name(nom_entreprise, prenom_dirigeant="", nom_dirigeant=""):
    """Tente de retrouver des emails par devination du domaine.

    Pipeline : nom → slugs candidats → tester SMTP catchall/MX sur chaque .fr/.com
    → si MX présent, tester direction@ / contact@ + email patterns dirigeant.

    Retourne dict : {site_web, email_dirigeant, email_dirigeant_confiance, strategiques}
    avec valeurs vides si rien trouvé.
    """
    result = {
        "site_web": "",
        "email_dirigeant": "",
        "email_dirigeant_confiance": "",
        "strategiques": [],
    }
    if not smtp_verifier.is_available():
        return result

    slugs = _guess_domain_candidates(nom_entreprise)
    if not slugs:
        return result

    for slug in slugs:
        for tld in [".fr", ".com"]:
            domain = slug + tld
            status = smtp_verifier.check_domain(domain)
            if status == "no_mx":
                continue  # pas d'email sur ce domaine
            if status not in ("ok", "catchall", "unknown"):
                continue

            # MX présent : on a une cible plausible. Construire le site_web canonique.
            site_web = "https://www." + domain
            result["site_web"] = site_web

            # Catchall : direction@ probable, fini
            if status == "catchall":
                result["strategiques"] = [
                    (("direction@" + domain), "direction", "probable"),
                ]
                logger.info("Guess domain catchall : %s", domain)
                return result

            # unknown : direction@ probable selon spec, fini
            if status == "unknown":
                result["strategiques"] = [
                    (("direction@" + domain), "direction", "probable"),
                    (("contact@" + domain), "contact", "probable"),
                ]
                logger.info("Guess domain unknown : %s", domain)
                # Si on a un dirigeant, ajouter aussi un pattern probable
                if prenom_dirigeant and nom_dirigeant:
                    pn_v = extract_nom_variants(nom_dirigeant)
                    pu = normalize_for_email(prenom_dirigeant.split()[0]) if prenom_dirigeant else ""
                    if pu and pn_v:
                        cand = _generate_email_candidates(pu, pn_v, domain)
                        if cand:
                            result["email_dirigeant"] = cand[0]
                            result["email_dirigeant_confiance"] = "probable"
                return result

            # status == "ok" : tester direction@ + contact@ + patterns dirigeant
            for prefix in ("direction", "contact"):
                check = smtp_verifier.verify_email(prefix + "@" + domain)
                if check == "valid":
                    typ = "direction" if prefix == "direction" else "contact"
                    result["strategiques"].append((prefix + "@" + domain, typ, "vérifié"))
                    break
                if check == "catchall":
                    typ = "direction" if prefix == "direction" else "contact"
                    result["strategiques"].append((prefix + "@" + domain, typ, "probable"))
                    break
                time.sleep(0.2)

            # Patterns dirigeant si dispo
            if prenom_dirigeant and nom_dirigeant and not result["email_dirigeant"]:
                pn_v = extract_nom_variants(nom_dirigeant)
                pu = normalize_for_email(prenom_dirigeant.split()[0]) if prenom_dirigeant else ""
                if pu and pn_v:
                    candidates = _generate_email_candidates(pu, pn_v, domain)
                    for email in candidates[:3]:
                        check = smtp_verifier.verify_email(email)
                        if check == "valid":
                            result["email_dirigeant"] = email
                            result["email_dirigeant_confiance"] = "vérifié"
                            break
                        if check == "catchall":
                            result["email_dirigeant"] = email
                            result["email_dirigeant_confiance"] = "probable"
                            break
                        time.sleep(0.2)

            # Si on a au moins une trouvaille, retourner
            if result["strategiques"] or result["email_dirigeant"]:
                logger.info("Guess domain success : %s pour %s", domain, nom_entreprise)
                return result

            # Sinon, on a juste un MX sans email exploitable → reset et essayer
            # le prochain slug
            result["site_web"] = ""

    return result


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
        # sans Debounce en fallback on classe direction@ + contact@ en probable.
        emails = [
            ("direction@" + domain, "direction", "probable"),
            ("contact@" + domain, "contact", "probable"),
        ]
        logger.info("Domaine unknown (%s) → 2 stratégiques probables", domain)
        return emails

    validated = []
    seen_types = set()
    consecutive_invalids = 0
    unknown_count = 0

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
        elif check == "unknown":
            # MX accepte mais RCPT ambigu (OVH/IONOS) → probable selon la spec.
            # On collecte le 1er par type pour avoir direction + contact
            unknown_count += 1
            if email_type not in seen_types:
                validated.append((email, email_type, "probable"))
                seen_types.add(email_type)
            # 3 unknowns consécutifs : tout sera unknown sur ce domaine, stop
            if unknown_count >= 3:
                break
        elif check == "invalid":
            # On ne break PAS sur invalids consécutifs : la liste contient
            # des préfixes "métier" (cabinet@, etude@, agence@) en fin de
            # liste qui sont parfois les SEULS valides sur des domaines
            # pro libéraux. Coût max ~17 × 0.2s = 3,4s par domaine.
            consecutive_invalids += 1
        elif check == "error":
            break

        time.sleep(0.2)

        # Couverture optimale : direction + (rh OU contact OU metier)
        # → on peut s'arrêter pour économiser
        if "direction" in seen_types and any(
            t in seen_types for t in ("rh", "contact", "metier")
        ):
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

    # Dernier recours : si pas de site OU site sans MX → tenter de deviner
    # un domaine candidat et le valider via SMTP service. Cap 3 slugs × 2 TLD.
    guess_result = None
    needs_guess = (not site_web)
    if not needs_guess and site_web:
        # Vérifier si le site connu est sans MX (pas exploitable pour l'email)
        try:
            d = extract_domain(site_web)
            if d and smtp_verifier.is_available() and smtp_verifier.check_domain(d) == "no_mx":
                needs_guess = True
                log_parts.append("site no_mx : tentative guess")
        except Exception:
            pass
    if needs_guess:
        prenom_dir = (entreprise.get("dirigeant_prenom") or "").strip()
        nom_dir = (entreprise.get("dirigeant_nom") or "").strip()
        try:
            guess_result = try_guess_emails_from_name(nom, prenom_dir, nom_dir)
        except Exception as e:
            logger.debug("guess emails erreur: %s", e)
            guess_result = None
        if guess_result and (guess_result.get("strategiques") or guess_result.get("email_dirigeant")):
            site_web = guess_result.get("site_web") or site_web
            log_parts.append("guess: " + (guess_result.get("site_web") or "?"))

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

    # Injecter les résultats du guess s'il a trouvé du dirigeant
    if guess_result and guess_result.get("email_dirigeant") and not email_dir:
        email_dir = guess_result["email_dirigeant"]
        email_dir_conf = guess_result.get("email_dirigeant_confiance", "")
        log_parts.append("email dir (guess): " + email_dir)

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
    # Compléter avec le guess (n'écrase pas les stratégiques déjà trouvés)
    if guess_result and guess_result.get("strategiques"):
        existing_emails = {e[0] for e in strategic if len(e) >= 1}
        for tup in guess_result["strategiques"]:
            if len(tup) >= 1 and tup[0] not in existing_emails:
                strategic.append(tup)
        if guess_result["strategiques"]:
            log_parts.append("strat (guess): " + str(len(guess_result["strategiques"])))
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
                # NE PAS supprimer l'email — l'utilisateur veut voir ce qu'on a
                # trouvé même si SMTP dit invalide. Marquer comme "incertain"
                # pour que l'UI puisse afficher un badge orange.
                scraped_conf = "incertain"
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
