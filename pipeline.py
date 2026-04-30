"""Pipeline d'exécution d'une recherche, réutilisable depuis l'UI ou le worker.

PRINCIPE DE BASE — IMPORTANT
============================
Le pipeline ENRICHIT les entreprises mais n'en supprime JAMAIS aucune.
Tous les filtres (email_requis, etc.) sont à appliquer à l'AFFICHAGE, pas ici.
Un échec d'enrichissement (SMTP unknown, site no_mx, etc.) ajoute juste un
champ de statut à côté — l'entreprise reste dans la liste avec ses autres
données (téléphone, site, etc.) intactes.

Ce principe vient d'un retour utilisateur : avoir 180 résultats au scraping
et n'en récupérer que 44 après enrichissement à cause d'un drop silencieux
sur "email_requis" est inacceptable. Mieux vaut 180 résultats avec des
champs partiellement remplis qu'une liste tronquée.

Étapes (poids du temps approximatif) :
  1. Scraping Google Maps               (45 %)
  2. enrich_emails (scraping sites)     (15 %)
  3. validate_scraped_emails (SMTP)     ( 5 %)
  4. enrich_entreprises (API gouv)      (10 %)
  5. enrich_nominative_emails (patterns)( 5 %)
  6. enrich_advanced (Perplexity)       (15 %)
  7. save_search                        ( 5 %)
"""

import logging

from scraper import scrape_google_maps
from email_enricher import enrich_emails
from email_finder import enrich_nominative_emails, validate_scraped_emails
from entreprise_enricher import enrich_entreprises
from advanced_enrichment import enrich_advanced
from scoring import calculate_scores
from validators import (
    is_annuaire_domain,
    is_parent_group_site,
    validate_site_matches_company,
)
import database

logger = logging.getLogger(__name__)


def _stage_callback(global_callback, stage_start, stage_end):
    """Wrappe un progress_callback en remappant ratio [0,1] → [start,end] global."""
    if global_callback is None:
        return None

    def cb(msg, ratio):
        try:
            ratio = max(0.0, min(1.0, float(ratio)))
        except Exception:
            ratio = 0.0
        global_ratio = stage_start + (stage_end - stage_start) * ratio
        global_callback(msg, global_ratio)
    return cb


def _validate_initial_sites(entreprises, progress_callback=None):
    """Étape 1.5 : invalide les sites GMaps qui ne pointent pas vers
    l'entreprise réelle (annuaires, groupes parents, sites non-cohérents).

    Évite que enrich_emails (Phase 1) perde du temps à scraper ces sites.
    Garde l'URL d'origine dans `site_web_original` pour traçabilité, et
    pose `site_web_rejected_reason` ∈ {"annuaire", "groupe_parent", "non_match"}.
    Le site_web validé reste, les autres sont vidés (Phase 2 Perplexity les
    re-cherchera proprement).
    """
    total = len(entreprises)
    for idx, e in enumerate(entreprises):
        if progress_callback and idx % 10 == 0:
            progress_callback("Validation site %d/%d" % (idx + 1, total),
                              (idx + 1) / max(total, 1))
        site = (e.get("site_web") or "").strip()
        if not site:
            continue
        nom = (e.get("nom") or "").strip()
        if not nom:
            continue

        # 1. Annuaire connu (pagesjaunes, lesgarages, e-pro, etc.)
        if is_annuaire_domain(site):
            e["site_web_original"] = site
            e["site_web"] = ""
            e["site_web_rejected_reason"] = "annuaire"
            continue

        # 2. Site groupe parent (renault.fr pour BERTHIAND AUTOMOBILES)
        if is_parent_group_site(site, nom):
            e["site_web_original"] = site
            e["site_web"] = ""
            e["site_web_rejected_reason"] = "groupe_parent"
            continue

        # 3. Validation lexicale + scraping page (le plus coûteux : 1 fetch HTTP
        # de 8ko avec timeout 4s par site). On garde le site si on ne peut pas
        # statuer (pas de connexion → on ne pénalise pas).
        try:
            ok = validate_site_matches_company(site, nom, timeout=4)
        except Exception:
            ok = True  # en cas de timeout/erreur réseau, on garde
        if not ok:
            e["site_web_original"] = site
            e["site_web"] = ""
            e["site_web_rejected_reason"] = "non_match"
    return entreprises


def _count_with_email(results):
    """Combien d'entreprises ont au moins un email (scrapé, dirigeant ou stratégique)."""
    n = 0
    for e in results:
        if (e.get("emails") or "").strip():
            n += 1
            continue
        if (e.get("email_dirigeant") or "").strip():
            n += 1
            continue
        if e.get("emails_strategiques"):
            n += 1
    return n


def _has_quality_email(e):
    """True si l'entreprise a un email de qualité prospection (vérifié/probable)
    en source dirigeant, direction ou stratégique. Sert à décider si Perplexity
    doit intervenir en Phase 2.
    """
    # Email scrapé statut SMTP
    if e.get("email_status") == "valid":
        return True
    # Email dirigeant déjà trouvé
    if (e.get("email_dirigeant") or "").strip():
        conf = e.get("email_dirigeant_confiance") or e.get("email_dirigeant_confidence") or ""
        if conf in ("vérifié", "probable", "high", "medium"):
            return True
    return False


def run_search(params: dict, progress_callback=None) -> dict:
    """Exécute le pipeline complet pour une recherche, en 2 phases.

    PHASE 1 — gratuit, sur TOUTES les entreprises :
      1. Scraping Google Maps                   (35 %)
      2. Validation des sites GMaps             ( 5 %)
         (rejette annuaires, groupes parents, sites non-cohérents)
      3. API gouv → dirigeant                   (10 %)
      4. Scraping HTML des sites validés        (15 %)
         (trouve contact@, /equipe, /mentions-legales)
      5. Validation SMTP des emails scrapés     ( 5 %)
      6. Patterns email dirigeant + SMTP        ( 5 %)
         (utile pour ceux qui ont dirigeant + site mais pas d'email scrapé)

    PHASE 2 — Perplexity, sur les manquantes uniquement, jusqu'à advanced_max :
      7. enrich_advanced                        (20 %)
         (Perplexity company → vrai site quand site_web absent ;
          Perplexity dirigeant → email perso ; emails stratégiques)

      → Économise des tokens car ne tourne que là où la Phase 1 a échoué.

      8. save_search                            ( 5 %)
    """
    activite = params.get("activite", "")
    zone = params.get("zone", "")
    stats = {}
    log_prefix = "Pipeline [{} / {}]".format(activite, zone)
    use_advanced = params.get("enrich_advanced", False)

    # ====================== PHASE 1 — gratuit ======================

    # --- 1. Scraping (35 %) ---
    cb = _stage_callback(progress_callback, 0.0, 0.35)
    if cb:
        cb("Scraping Google Maps...", 0.0)
    results = scrape_google_maps(
        activite=activite,
        zone=zone,
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

    # --- 2. Validation des sites GMaps (5 %) ---
    cb = _stage_callback(progress_callback, 0.35, 0.40)
    if cb:
        cb("Validation des sites web...", 0.0)
    results = _validate_initial_sites(results, progress_callback=cb)
    assert len(results) == n_initial
    stats["sites_rejetes"] = sum(1 for e in results if e.get("site_web_rejected_reason"))
    stats["sites_valides"] = sum(1 for e in results if (e.get("site_web") or "").strip())
    logger.info(
        "%s validation sites → %d valides, %d rejetés (annuaire/groupe/non-match)",
        log_prefix, stats["sites_valides"], stats["sites_rejetes"],
    )

    # --- 3. API gouv → dirigeant (10 %) ---
    if params.get("search_dirigeants", True):
        cb = _stage_callback(progress_callback, 0.40, 0.50)
        results = enrich_entreprises(results, progress_callback=cb)
        assert len(results) == n_initial
        stats["dirigeants_trouves"] = sum(
            1 for e in results if (e.get("dirigeant_prenom") or e.get("dirigeant_nom"))
        )
        logger.info("%s API gouv → %d dirigeants", log_prefix, stats["dirigeants_trouves"])

    # --- 4. Scraping HTML des sites VALIDÉS (15 %) ---
    if params.get("search_emails", True):
        cb = _stage_callback(progress_callback, 0.50, 0.65)
        results = enrich_emails(results, progress_callback=cb)
        assert len(results) == n_initial
        stats["with_email_after_scraping"] = sum(
            1 for e in results if (e.get("emails") or "").strip()
        )
        logger.info(
            "%s scraping HTML → %d emails", log_prefix, stats["with_email_after_scraping"],
        )

    # --- 5. Validation SMTP des emails scrapés (5 %) ---
    if params.get("validate_emails", True) and params.get("search_emails", True):
        cb = _stage_callback(progress_callback, 0.65, 0.70)
        results = validate_scraped_emails(results, progress_callback=cb)
        assert len(results) == n_initial
        stats["valid_emails"] = sum(1 for e in results if e.get("email_status") == "valid")
        stats["catchall_emails"] = sum(1 for e in results if e.get("email_status") == "catchall")
        logger.info(
            "%s SMTP scraped → %d valid, %d catchall",
            log_prefix, stats["valid_emails"], stats["catchall_emails"],
        )

    # --- 6. Patterns email dirigeant + SMTP (5 %) ---
    # Toujours actif : c'est gratuit (le service VPS) et ça complète le scraping.
    # On ne le fait pas si advanced est activé : enrich_advanced fait pareil en mieux.
    if (params.get("search_dirigeants", True)
            and params.get("enrich_nominative", True)
            and not use_advanced):
        cb = _stage_callback(progress_callback, 0.70, 0.75)
        results = enrich_nominative_emails(results, progress_callback=cb)
        assert len(results) == n_initial
        stats["email_dir_phase1"] = sum(
            1 for e in results if (e.get("email_dirigeant") or "").strip()
        )
        logger.info("%s patterns dirigeant → %d emails", log_prefix, stats["email_dir_phase1"])

    # ====================== PHASE 2 — Perplexity (sélectif) ======================
    if use_advanced:
        # Sélectionner les entreprises sans email de qualité, triées par score
        # (les plus prometteuses d'abord), cap à advanced_max
        candidates = [e for e in results if not _has_quality_email(e)]
        candidates.sort(key=lambda e: e.get("score", 0), reverse=True)
        cap = params.get("advanced_max")
        if cap is not None and cap > 0:
            candidates = candidates[: int(cap)]
        stats["perplexity_targets"] = len(candidates)
        logger.info(
            "%s Phase 2 Perplexity : %d entreprises ciblées (sur %d sans email qualité)",
            log_prefix, len(candidates), sum(1 for e in results if not _has_quality_email(e)),
        )

        if candidates:
            cb = _stage_callback(progress_callback, 0.75, 0.95)
            # enrich_advanced modifie en place les dicts → on lui passe la sous-liste
            # mais les changements sont reflétés dans `results` (mêmes références).
            enrich_advanced(
                candidates,
                progress_callback=cb,
                do_perplexity=params.get("do_perplexity", True),
                do_strategic=params.get("do_strategic", True),
                max_entreprises=None,  # on a déjà cappé en amont
            )
            assert len(results) == n_initial, "enrich_advanced a modifié results !"
            stats["email_dir_phase2"] = sum(
                1 for e in candidates if (e.get("email_dirigeant") or "").strip()
            )
            stats["with_strategic_phase2"] = sum(
                1 for e in candidates if e.get("emails_strategiques")
            )
            logger.info(
                "%s Phase 2 → %d emails dirigeants, %d avec stratégique",
                log_prefix, stats["email_dir_phase2"], stats["with_strategic_phase2"],
            )

    # Scoring (n'altère que le champ score)
    results = calculate_scores(results)

    # --- Filtre final "uniquement avec email" ---
    # Appliqué APRÈS tout l'enrichissement. Toutes les entreprises ont eu leur
    # chance (Phase 1 gratuite + Phase 2 Perplexity). Si le user a coché la
    # case, on ne sauvegarde que celles avec un email final.
    n_before_filter = len(results)
    stats["before_email_filter"] = n_before_filter
    if params.get("email_requis", False):
        results = [r for r in results if _count_with_email([r]) > 0]
        stats["dropped_no_email"] = n_before_filter - len(results)
        logger.info(
            "%s filtre email_requis : %d entreprises sans email droppées (sur %d)",
            log_prefix, stats["dropped_no_email"], n_before_filter,
        )

    # --- 8. save_search ---
    if progress_callback:
        progress_callback("Sauvegarde...", 0.97)
    search_id = database.save_search(activite, zone, params, results)
    stats["final"] = len(results)
    stats["with_any_email"] = _count_with_email(results)
    logger.info(
        "%s TERMINÉ : %d entreprises sauvegardées, %d avec email",
        log_prefix, stats["final"], stats["with_any_email"],
    )
    if progress_callback:
        progress_callback(
            "Terminé : {} entreprise(s), {} avec email".format(
                stats["final"], stats["with_any_email"],
            ),
            1.0,
        )
    return {
        "search_id": search_id,
        "results": results,
        "count": len(results),
        "stats": stats,
    }
