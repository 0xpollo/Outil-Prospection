"""Pipeline d'enrichissement v2 (mai 2026).

OBJECTIF UNIQUE : maximiser le taux d'emails non-bounce dans la liste finale
qui passera à Debounce. La quantité n'a aucune valeur.

PRINCIPES INVIOLABLES
=====================
1. Le pipeline n'enlève JAMAIS d'entreprise (asserts crashent le worker sinon).
   Une entreprise sans email reste dans la liste, classée P2 (recherche manuelle).
2. Aucun email inventé n'est conservé sans preuve SMTP "valid". Sur un MX
   opaque (OVH/IONOS/CleanMail/…), on ne génère AUCUN pattern : trop de
   bounces.
3. Les filtres (`email_requis`, etc.) ne s'appliquent qu'à la sauvegarde
   finale, jamais pendant l'enrichissement.

ÉTAPES
======
  1. Scraping Google Maps                                  (30 %)
  2. Validation des sites GMaps                            ( 5 %)
  3. (advanced) Perplexity → site pour les manquants       ( 5 %)
  4. API gouv → dirigeants                                 (10 %)
  5. MX classification (par domaine, parallèle)            ( 5 %)
  6. Scraping HTML des sites validés → emails publiés      (15 %)
  7. SMTP validation des emails publiés                    ( 5 %)
  8. (advanced) Génération de patterns SI MX discriminatif
     ET dirigeant connu ET aucun email valid encore         (15 %)
  9. Calcul rang destinataire + tier P0/P1/P2/X            ( 5 %)
 10. Save                                                  ( 5 %)
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import database
import mx_classifier
import perplexity_search
import smtp_verifier
from scraper import scrape_google_maps
from email_enricher import enrich_emails
from email_processor import (
    PUBLIC_EMAIL_DOMAINS,
    classify_tier,
    assign_destinataire_ranks,
    is_public_email,
)
from entreprise_enricher import enrich_entreprises
from pattern_finder import find_dirigeant_email, find_generic_email
from scoring import calculate_scores
from validators import (
    extract_domain,
    is_annuaire_domain,
    is_parent_group_site,
    validate_site_matches_company,
)

logger = logging.getLogger(__name__)


def _stage_callback(global_cb, start, end):
    if global_cb is None:
        return None

    def cb(msg, ratio):
        try:
            ratio = max(0.0, min(1.0, float(ratio)))
        except Exception:
            ratio = 0.0
        global_cb(msg, start + (end - start) * ratio)
    return cb


def _validate_initial_sites(entreprises, progress_callback=None):
    """Rejette les sites GMaps qui ne pointent pas sur l'entreprise réelle."""
    total = len(entreprises)
    for idx, e in enumerate(entreprises):
        if progress_callback and idx % 10 == 0:
            progress_callback("Validation site %d/%d" % (idx + 1, total),
                              (idx + 1) / max(total, 1))
        site = (e.get("site_web") or "").strip()
        nom = (e.get("nom") or "").strip()
        if not site or not nom:
            continue
        if is_annuaire_domain(site):
            e["site_web_original"] = site
            e["site_web"] = ""
            e["site_web_rejected_reason"] = "annuaire"
            continue
        if is_parent_group_site(site, nom):
            e["site_web_original"] = site
            e["site_web"] = ""
            e["site_web_rejected_reason"] = "groupe_parent"
            continue
        try:
            ok = validate_site_matches_company(site, nom, timeout=4)
        except Exception:
            ok = True
        if not ok:
            e["site_web_original"] = site
            e["site_web"] = ""
            e["site_web_rejected_reason"] = "non_match"
    return entreprises


def _find_missing_sites_perplexity(entreprises, progress_callback=None):
    """Pour chaque entreprise sans site_web, Perplexity Sonar → site officiel.
    Le site est validé lexicalement par perplexity_search avant retour."""
    if not perplexity_search.is_available():
        return
    todo = [e for e in entreprises if not (e.get("site_web") or "").strip()]
    total = len(todo)
    if total == 0:
        return

    def _one(e):
        nom = e.get("nom") or ""
        lieu = e.get("adresse") or e.get("ville") or ""
        try:
            res = perplexity_search.search_company(nom, lieu)
        except Exception as exc:
            logger.debug("Perplexity company %s : %s", nom, exc)
            return e, None
        return e, res

    completed = [0]
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(_one, e) for e in todo]
        for fut in as_completed(futures):
            try:
                e, res = fut.result()
            except Exception:
                e, res = None, None
            with lock:
                completed[0] += 1
                done = completed[0]
            if progress_callback:
                progress_callback(
                    "Site Perplexity %d/%d" % (done, total),
                    done / max(total, 1),
                )
            if e is None or not res:
                continue
            site = (res.get("site_web") or "").strip()
            if site:
                e["site_web"] = site


def _classify_mx_all(entreprises, progress_callback=None):
    """Classifie le MX de chaque domaine unique (parallèle).
    Écrit `mx_provider` et `mx_type` sur chaque entreprise."""
    domains = {}
    for e in entreprises:
        site = (e.get("site_web") or "").strip()
        d = extract_domain(site) if site else ""
        if d:
            domains.setdefault(d, []).append(e)
    total = len(domains)
    if total == 0:
        return

    def _one(domain):
        try:
            return domain, mx_classifier.classify(domain)
        except Exception as exc:
            logger.debug("MX classify %s : %s", domain, exc)
            return domain, ("", "")

    completed = [0]
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(_one, d) for d in domains.keys()]
        for fut in as_completed(futures):
            domain, (provider, mx_type) = fut.result()
            for e in domains[domain]:
                e["mx_provider"] = provider
                e["mx_type"] = mx_type
            with lock:
                completed[0] += 1
                done = completed[0]
            if progress_callback:
                progress_callback("MX %d/%d" % (done, total), done / max(total, 1))


def _validate_published_emails(entreprises, progress_callback=None):
    """Pour chaque email publié non-public, /verify via SMTP service.
    Drop l'email si SMTP confirme invalid/no_mx ; conserve sinon."""
    to_check = []
    for e in entreprises:
        for em in (e.get("emails") or []):
            if em.get("is_public_domain"):
                continue
            if em.get("smtp_status"):
                continue
            to_check.append(em)
    total = len(to_check)
    if total == 0:
        return

    # Dédupliquer par email pour économiser des appels
    unique = {}
    for em in to_check:
        unique.setdefault(em["email"], []).append(em)
    uniq_keys = list(unique.keys())

    def _check(email):
        try:
            return email, smtp_verifier.verify_email(email)
        except Exception:
            return email, "error"

    completed = [0]
    lock = threading.Lock()
    results = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(_check, email) for email in uniq_keys]
        for fut in as_completed(futures):
            email, status = fut.result()
            results[email] = status
            with lock:
                completed[0] += 1
                done = completed[0]
            if progress_callback:
                progress_callback(
                    "SMTP %d/%d" % (done, len(uniq_keys)),
                    done / max(len(uniq_keys), 1),
                )

    # Propager les résultats. Drop les invalid / no_mx.
    dead = {"invalid", "no_mx", "invalid_format"}
    for e in entreprises:
        new_list = []
        for em in (e.get("emails") or []):
            if em.get("is_public_domain"):
                new_list.append(em)
                continue
            if em.get("smtp_status"):
                new_list.append(em)
                continue
            status = results.get(em["email"], "")
            if status in dead:
                continue
            em["smtp_status"] = status
            new_list.append(em)
        e["emails"] = new_list


def _has_valid_corporate_email(e):
    """True si l'entreprise a déjà un email pro (non-public) avec un SMTP
    `valid` ou `catchall` — auquel cas pas la peine de générer des patterns."""
    for em in (e.get("emails") or []):
        if em.get("is_public_domain"):
            continue
        status = (em.get("smtp_status") or "").lower()
        if status in ("valid", "catchall"):
            return True
    return False


def _generate_patterns_phase(entreprises, progress_callback=None):
    """Pour les entreprises éligibles, génère et SMTP-vérifie patterns dirigeant
    et/ou générique. RÈGLE : uniquement sur MX discriminatif, et on ne garde
    que ceux qui reviennent `valid`.
    """
    candidates = []
    for e in entreprises:
        if e.get("mx_type") != "discriminatif":
            continue
        if _has_valid_corporate_email(e):
            continue
        site = (e.get("site_web") or "").strip()
        domain = extract_domain(site) if site else ""
        if not domain:
            continue
        candidates.append((e, domain))

    total = len(candidates)
    if total == 0:
        return

    def _one(entry):
        e, domain = entry
        prenom = (e.get("dirigeant_prenom") or "").strip()
        nom = (e.get("dirigeant_nom") or "").strip()
        site = (e.get("site_web") or "").strip()
        found = None
        if prenom and nom:
            try:
                found = find_dirigeant_email(prenom, nom, domain, source_url=site)
            except Exception as exc:
                logger.debug("find_dirigeant_email %s : %s", e.get("nom"), exc)
        if found is None:
            try:
                found = find_generic_email(domain, source_url=site)
            except Exception as exc:
                logger.debug("find_generic_email %s : %s", e.get("nom"), exc)
        return e, found

    completed = [0]
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(_one, c) for c in candidates]
        for fut in as_completed(futures):
            try:
                e, found = fut.result()
            except Exception:
                e, found = None, None
            with lock:
                completed[0] += 1
                done = completed[0]
            if progress_callback:
                progress_callback(
                    "Patterns %d/%d" % (done, total), done / max(total, 1),
                )
            if e is None or found is None:
                continue
            # Évite les doublons
            if not any(em.get("email") == found["email"]
                       for em in (e.get("emails") or [])):
                e.setdefault("emails", []).append(found)


def _has_any_email(e):
    return bool(e.get("emails"))


def run_search(params, progress_callback=None):
    """Exécute le pipeline complet pour une recherche. Retourne dict avec
    search_id, results, count, stats."""
    activite = params.get("activite", "")
    zone = params.get("zone", "")
    stats = {}
    log_prefix = "Pipeline [%s / %s]" % (activite, zone)
    use_advanced = params.get("enrich_advanced", False)

    # Reset des caches inter-runs
    smtp_verifier.reset_cache()
    mx_classifier.reset_cache()

    # --- 1. Scraping (30 %) ---
    cb = _stage_callback(progress_callback, 0.0, 0.30)
    if cb:
        cb("Scraping Google Maps...", 0.0)
    results = scrape_google_maps(
        activite=activite, zone=zone,
        max_results=params.get("max_results", 20),
        note_minimum=params.get("note_minimum", 0.0),
        nb_avis_minimum=params.get("nb_avis_minimum", 0),
        telephone_requis=params.get("telephone_requis", False),
        portable_uniquement=params.get("portable_uniquement", False),
        site_web_requis=params.get("site_web_requis", False),
        code_postal=params.get("code_postal", ""),
        geo_lat=params.get("geo_lat"),
        geo_lng=params.get("geo_lng"),
        mode=params.get("mode", "simple"),
        progress_callback=cb,
    )
    stats["scraping"] = len(results)
    logger.info("%s scraping → %d entreprises", log_prefix, len(results))

    if not results:
        if progress_callback:
            progress_callback("Aucun résultat", 1.0)
        search_id = database.save_search(activite, zone, params, [])
        return {"search_id": search_id, "results": [], "count": 0, "stats": stats}

    n_initial = len(results)
    # Initialiser la liste emails vide partout
    for e in results:
        e.setdefault("emails", [])

    # --- 2. Validation sites (5 %) ---
    cb = _stage_callback(progress_callback, 0.30, 0.35)
    if cb:
        cb("Validation des sites web...", 0.0)
    _validate_initial_sites(results, progress_callback=cb)
    assert len(results) == n_initial
    stats["sites_rejetes"] = sum(1 for e in results if e.get("site_web_rejected_reason"))
    stats["sites_valides"] = sum(1 for e in results if (e.get("site_web") or "").strip())

    # --- 3. Perplexity site pour les manquants (advanced uniquement) ---
    if use_advanced:
        cb = _stage_callback(progress_callback, 0.35, 0.40)
        _find_missing_sites_perplexity(results, progress_callback=cb)
        assert len(results) == n_initial
        stats["sites_perplexity"] = sum(
            1 for e in results
            if (e.get("site_web") or "").strip() and not e.get("site_web_rejected_reason")
        )

    # --- 4. API gouv → dirigeants (10 %) ---
    if params.get("search_dirigeants", True):
        cb = _stage_callback(progress_callback, 0.40, 0.50)
        enrich_entreprises(results, progress_callback=cb)
        assert len(results) == n_initial
        stats["dirigeants_trouves"] = sum(
            1 for e in results
            if (e.get("dirigeant_prenom") or e.get("dirigeant_nom"))
        )

    # --- 5. MX classification (5 %) ---
    cb = _stage_callback(progress_callback, 0.50, 0.55)
    _classify_mx_all(results, progress_callback=cb)
    assert len(results) == n_initial
    stats["mx_discriminatif"] = sum(1 for e in results if e.get("mx_type") == "discriminatif")
    stats["mx_opaque"] = sum(1 for e in results if e.get("mx_type") == "opaque")
    stats["mx_no_mx"] = sum(1 for e in results if e.get("mx_type") == "no_mx")
    logger.info(
        "%s MX → %d discr / %d opaque / %d no_mx",
        log_prefix, stats["mx_discriminatif"], stats["mx_opaque"], stats["mx_no_mx"],
    )

    # --- 6. Scraping HTML → emails publiés (15 %) ---
    if params.get("search_emails", True):
        cb = _stage_callback(progress_callback, 0.55, 0.70)
        enrich_emails(results, progress_callback=cb)
        assert len(results) == n_initial
        stats["with_published_email"] = sum(
            1 for e in results if e.get("emails")
        )

    # --- 7. SMTP des emails publiés (5 %) ---
    if params.get("validate_emails", True) and params.get("search_emails", True):
        cb = _stage_callback(progress_callback, 0.70, 0.75)
        _validate_published_emails(results, progress_callback=cb)
        assert len(results) == n_initial
        stats["smtp_valid"] = sum(
            1 for e in results for em in e.get("emails", [])
            if em.get("smtp_status") == "valid"
        )

    # --- 8. Génération patterns sur MX discriminatif (15 %) ---
    if use_advanced:
        cb = _stage_callback(progress_callback, 0.75, 0.90)
        _generate_patterns_phase(results, progress_callback=cb)
        assert len(results) == n_initial
        stats["pattern_found"] = sum(
            1 for e in results for em in e.get("emails", [])
            if em.get("source") in ("pattern", "generic")
        )

    # --- 9. Rang destinataire + tier (5 %) ---
    if progress_callback:
        progress_callback("Classification finale...", 0.92)
    for e in results:
        assign_destinataire_ranks(e)
        e["tier"] = classify_tier(e)
    results = calculate_scores(results)

    stats["tier_P0"] = sum(1 for e in results if e.get("tier") == "P0")
    stats["tier_P1"] = sum(1 for e in results if e.get("tier") == "P1")
    stats["tier_P2"] = sum(1 for e in results if e.get("tier") == "P2")
    stats["tier_X"] = sum(1 for e in results if e.get("tier") == "X")

    # --- Filtre final email_requis ---
    n_before = len(results)
    stats["before_email_filter"] = n_before
    if params.get("email_requis", False):
        results = [r for r in results if _has_any_email(r)]
        stats["dropped_no_email"] = n_before - len(results)

    # --- 10. Save ---
    if progress_callback:
        progress_callback("Sauvegarde...", 0.97)
    search_id = database.save_search(activite, zone, params, results)
    stats["final"] = len(results)
    logger.info(
        "%s TERMINÉ : %d entreprises (P0=%d, P1=%d, P2=%d, X=%d)",
        log_prefix, stats["final"], stats["tier_P0"], stats["tier_P1"],
        stats["tier_P2"], stats["tier_X"],
    )
    if progress_callback:
        progress_callback(
            "Terminé : %d entreprises (P0+P1 envoi=%d)" % (
                stats["final"], stats["tier_P0"] + stats["tier_P1"],
            ),
            1.0,
        )
    return {
        "search_id": search_id, "results": results,
        "count": len(results), "stats": stats,
    }
