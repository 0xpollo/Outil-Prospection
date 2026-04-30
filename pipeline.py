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


def run_search(params: dict, progress_callback=None) -> dict:
    """Exécute le pipeline complet pour une recherche.

    params (dict) : voir app.py / worker.py — toutes les clés sont optionnelles
    sauf activite/zone. Voir aussi le commentaire de tête du fichier sur le
    principe "ENRICHIT, ne supprime jamais".

    Retourne : {"search_id": int, "results": list, "count": int, "stats": dict}
    où stats contient un compte par étape (utile pour debug et UI).
    """
    activite = params.get("activite", "")
    zone = params.get("zone", "")
    stats = {}
    log_prefix = "Pipeline [{} / {}]".format(activite, zone)

    # --- Étape 1 : scraping (45 %) ---
    scrape_cb = _stage_callback(progress_callback, 0.0, 0.45)
    if scrape_cb:
        scrape_cb("Démarrage du scraping...", 0.0)

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
        progress_callback=scrape_cb,
    )

    stats["scraping"] = len(results)
    logger.info("%s scraping → %d entreprise(s)", log_prefix, len(results))

    if not results:
        if progress_callback:
            progress_callback("Aucun résultat", 1.0)
        search_id = database.save_search(activite, zone, params, [])
        return {"search_id": search_id, "results": [], "count": 0, "stats": stats}

    n_initial = len(results)

    # --- Étape 2 : enrich_emails (scraping sites) (15 %) ---
    if params.get("search_emails", True):
        cb = _stage_callback(progress_callback, 0.45, 0.60)
        results = enrich_emails(results, progress_callback=cb)
        # Vérification : on ne doit PAS perdre d'entreprises
        assert len(results) == n_initial, "enrich_emails a modifié le nombre d'entreprises !"
        stats["with_email_after_scraping"] = sum(
            1 for e in results if (e.get("emails") or "").strip()
        )
        logger.info(
            "%s enrich_emails → %d/%d avec email", log_prefix,
            stats["with_email_after_scraping"], n_initial,
        )

    # --- Étape 3 : validate_scraped_emails (5 %) ---
    if params.get("validate_emails", True) and params.get("search_emails", True):
        cb = _stage_callback(progress_callback, 0.60, 0.65)
        results = validate_scraped_emails(results, progress_callback=cb)
        assert len(results) == n_initial, "validate_scraped_emails a modifié le nombre d'entreprises !"
        stats["valid_emails"] = sum(
            1 for e in results if e.get("email_status") == "valid"
        )
        stats["catchall_emails"] = sum(
            1 for e in results if e.get("email_status") == "catchall"
        )
        logger.info(
            "%s validate → %d valid, %d catchall sur %d", log_prefix,
            stats["valid_emails"], stats["catchall_emails"], n_initial,
        )

    # NB : email_requis n'est PAS appliqué ici. C'est un filtre d'affichage, pas
    # un filtre de pipeline. Cf. principe en tête de fichier.

    # --- Étape 4 : enrich_entreprises (API gouv) (10 %) ---
    if params.get("search_dirigeants", True):
        cb = _stage_callback(progress_callback, 0.65, 0.75)
        results = enrich_entreprises(results, progress_callback=cb)
        assert len(results) == n_initial, "enrich_entreprises a modifié le nombre d'entreprises !"
        stats["dirigeants_trouves"] = sum(
            1 for e in results if (e.get("dirigeant_prenom") or e.get("dirigeant_nom"))
        )
        logger.info(
            "%s enrich_entreprises → %d/%d dirigeants", log_prefix,
            stats["dirigeants_trouves"], n_initial,
        )

    # --- Étape 5 : enrich_nominative_emails (5 %) ---
    # Désactivé si advanced est activé (advanced couvre la même chose en mieux)
    if (params.get("enrich_nominative", True)
            and params.get("search_dirigeants", True)
            and not params.get("enrich_advanced", False)):
        cb = _stage_callback(progress_callback, 0.75, 0.80)
        results = enrich_nominative_emails(results, progress_callback=cb)
        assert len(results) == n_initial, "enrich_nominative a modifié le nombre d'entreprises !"
        stats["email_dirigeant_nominative"] = sum(
            1 for e in results if (e.get("email_dirigeant") or "").strip()
        )
        logger.info(
            "%s enrich_nominative → %d/%d emails dirigeants", log_prefix,
            stats["email_dirigeant_nominative"], n_initial,
        )

    # --- Étape 6 : enrich_advanced (Perplexity + emails stratégiques) (15 %) ---
    if params.get("enrich_advanced", False):
        cb = _stage_callback(progress_callback, 0.80, 0.95)
        results = enrich_advanced(
            results,
            progress_callback=cb,
            do_perplexity=params.get("do_perplexity", True),
            do_strategic=params.get("do_strategic", True),
            max_entreprises=params.get("advanced_max"),
        )
        assert len(results) == n_initial, "enrich_advanced a modifié le nombre d'entreprises !"
        stats["email_dirigeant_advanced"] = sum(
            1 for e in results if (e.get("email_dirigeant") or "").strip()
        )
        stats["with_strategic"] = sum(
            1 for e in results if e.get("emails_strategiques")
        )
        logger.info(
            "%s enrich_advanced → %d emails dirigeants, %d avec stratégique sur %d",
            log_prefix,
            stats["email_dirigeant_advanced"],
            stats["with_strategic"], n_initial,
        )

    # Scoring (n'altère que le champ score)
    results = calculate_scores(results)

    # --- Étape 7 : save_search (5 %) ---
    if progress_callback:
        progress_callback("Sauvegarde...", 0.97)
    search_id = database.save_search(activite, zone, params, results)

    stats["final"] = len(results)
    stats["with_any_email"] = _count_with_email(results)
    logger.info(
        "%s TERMINÉ : %d entreprises sauvegardées (%d avec email)",
        log_prefix, stats["final"], stats["with_any_email"],
    )

    if progress_callback:
        progress_callback(
            "Terminé : {} entreprise(s) sauvegardée(s), {} avec email".format(
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
