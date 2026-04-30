"""Script de test du pipeline d'enrichissement avancé sur 3 requêtes réelles.

Usage : python3 run_test.py [--limit 20] [--query "plombier Lyon"]

Sans argument : lance les 3 requêtes de la spec et affiche les métriques.
"""

import argparse
import json
import logging
import os
import sys
import time

from scraper import _create_http_session, _http_fetch_businesses
from email_enricher import enrich_emails
from entreprise_enricher import enrich_entreprises
from advanced_enrichment import enrich_advanced
import smtp_verifier
import perplexity_search


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Réduire le bruit
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)


REQUETES = [
    {"label": "plombier Lyon",         "query": "plombier",         "lat": 45.764,  "lng": 4.835},
    {"label": "garagiste Marseille",   "query": "garagiste",        "lat": 43.2965, "lng": 5.3698},
    {"label": "expert-comptable Bordeaux", "query": "expert-comptable", "lat": 44.8378, "lng": -0.5792},
]


def has_email_value(ent):
    """Au moins un email présent (scrapé OU dirigeant OU stratégique)."""
    if (ent.get("emails") or "").strip():
        return True
    if (ent.get("email_dirigeant") or "").strip():
        return True
    if ent.get("emails_strategiques"):
        return True
    return False


def has_quality_email(ent):
    """Email vérifié OU probable (pas incertain ni sans confiance)."""
    qualities = {"vérifié", "probable"}
    if ent.get("email_dirigeant") and ent.get("email_dirigeant_confiance") in qualities:
        return True
    for e in ent.get("emails_strategiques") or []:
        # tuple (email, type, confiance)
        if len(e) >= 3 and e[2] in qualities:
            return True
    if ent.get("emails") and ent.get("emails_confiance") in qualities:
        return True
    return False


def count_qualities(entreprises):
    n_verifie = 0
    n_probable = 0
    for e in entreprises:
        confs = []
        if e.get("email_dirigeant"):
            confs.append(e.get("email_dirigeant_confiance", ""))
        if e.get("emails"):
            confs.append(e.get("emails_confiance", ""))
        for s in e.get("emails_strategiques") or []:
            if len(s) >= 3:
                confs.append(s[2])
        if "vérifié" in confs:
            n_verifie += 1
        elif "probable" in confs:
            n_probable += 1
    return n_verifie, n_probable


def run_one(label, query, lat, lng, limit=20, do_perplexity=True, do_strategic=True):
    print("\n" + "=" * 70)
    print("REQUETE : " + label + " (cap " + str(limit) + ")")
    print("=" * 70)

    t_start = time.time()

    # --- Étape 1 : scraping Google Maps HTTP ---
    session = _create_http_session()
    businesses = _http_fetch_businesses(session, query, lat, lng, max_results=limit * 2)
    if not businesses:
        print("ERREUR : aucune entreprise scrapée pour " + label)
        return None

    # Limiter à `limit` premiers résultats
    entreprises = businesses[:limit]
    n = len(entreprises)
    print("Scrapé : " + str(n) + " entreprises")

    # Baseline : combien ont déjà un site_web ?
    n_with_site = sum(1 for e in entreprises if e.get("site_web"))
    print("- avec site_web initial : " + str(n_with_site))

    # --- Étape 2 : enrichissement email basique (existant) ---
    t = time.time()
    entreprises = enrich_emails(entreprises)
    print("- enrich_emails (scraping sites) : " + str(round(time.time() - t, 1)) + "s")

    n_email_before = sum(1 for e in entreprises if has_email_value(e))
    print("AVANT advanced enrichment :")
    print("  - entreprises avec un email : " + str(n_email_before) + "/" + str(n))

    # --- Étape 3 : dirigeants via API gouv ---
    t = time.time()
    entreprises = enrich_entreprises(entreprises)
    n_dirigeant = sum(1 for e in entreprises if (e.get("dirigeant_prenom") or e.get("dirigeant_nom")))
    print("- enrich_entreprises (API gouv) : " + str(round(time.time() - t, 1)) + "s")
    print("  - dirigeants trouvés : " + str(n_dirigeant) + "/" + str(n))

    # --- Étape 4 : enrichissement avancé (Perplexity + GMaps + dirigeant + stratégique) ---
    t = time.time()
    entreprises = enrich_advanced(
        entreprises,
        do_perplexity=do_perplexity,
        do_strategic=do_strategic,
        max_entreprises=limit,
    )
    print("- enrich_advanced : " + str(round(time.time() - t, 1)) + "s")

    # Métriques après
    n_email_after = sum(1 for e in entreprises if has_email_value(e))
    n_quality = sum(1 for e in entreprises if has_quality_email(e))
    n_verifie, n_probable = count_qualities(entreprises)

    print("APRÈS advanced enrichment :")
    print("  - entreprises avec un email : " + str(n_email_after) + "/" + str(n))
    print("  - dont vérifié OU probable  : " + str(n_quality) + "/" + str(n) + " (" + str(round(100 * n_quality / max(n, 1))) + "%)")
    print("    - vérifié  : " + str(n_verifie))
    print("    - probable : " + str(n_probable))

    elapsed = time.time() - t_start
    print("Temps total : " + str(round(elapsed, 1)) + "s")

    return {
        "label": label,
        "query": query,
        "n": n,
        "with_email_before": n_email_before,
        "with_email_after": n_email_after,
        "verifie": n_verifie,
        "probable": n_probable,
        "quality_total": n_quality,
        "quality_pct": round(100 * n_quality / max(n, 1), 1),
        "elapsed_s": round(elapsed, 1),
        "samples": [
            {
                "nom": e.get("nom"),
                "site_web": e.get("site_web"),
                "dirigeant": (e.get("dirigeant_prenom", "") + " " + e.get("dirigeant_nom", "")).strip(),
                "email_dirigeant": e.get("email_dirigeant"),
                "email_dirigeant_confiance": e.get("email_dirigeant_confiance"),
                "emails_strategiques": e.get("emails_strategiques"),
                "emails": e.get("emails"),
                "emails_confiance": e.get("emails_confiance"),
                "log": e.get("enrichment_log"),
            }
            for e in entreprises[:limit]
        ],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--query", type=str, default=None, help="Ne tester qu'une requête")
    parser.add_argument("--no-perplexity", action="store_true")
    parser.add_argument("--no-strategic", action="store_true")
    parser.add_argument("--output", type=str, default="run_test_results.json")
    args = parser.parse_args()

    print("\n=== Sanity checks ===")
    print("SMTP verifier joignable :", smtp_verifier.is_available())
    print("Perplexity API key      :", "configurée" if perplexity_search.is_available() else "MANQUANTE")
    print()

    requetes = REQUETES
    if args.query:
        requetes = [r for r in REQUETES if r["label"].startswith(args.query) or r["query"] == args.query]
        if not requetes:
            print("Requête non trouvée : " + args.query)
            sys.exit(1)

    results = []
    for r in requetes:
        result = run_one(
            r["label"], r["query"], r["lat"], r["lng"],
            limit=args.limit,
            do_perplexity=not args.no_perplexity,
            do_strategic=not args.no_strategic,
        )
        if result:
            results.append(result)

    # Récap global
    print("\n" + "=" * 70)
    print("RÉCAP GLOBAL")
    print("=" * 70)
    total_n = sum(r["n"] for r in results)
    total_quality = sum(r["quality_total"] for r in results)
    total_before = sum(r["with_email_before"] for r in results)
    total_after = sum(r["with_email_after"] for r in results)
    print("Requete                       | N  | Avant | Après | Vérif | Prob | Quality % ")
    print("-" * 78)
    for r in results:
        print(
            ("{:<28}".format(r["label"][:28]))
            + " | " + ("{:>2}".format(r["n"]))
            + " | " + ("{:>5}".format(r["with_email_before"]))
            + " | " + ("{:>5}".format(r["with_email_after"]))
            + " | " + ("{:>5}".format(r["verifie"]))
            + " | " + ("{:>4}".format(r["probable"]))
            + " | " + ("{:>5}".format(str(r["quality_pct"]) + "%"))
        )
    print("-" * 78)
    pct_global = round(100 * total_quality / max(total_n, 1), 1)
    print(
        ("{:<28}".format("TOTAL"))
        + " | " + ("{:>2}".format(total_n))
        + " | " + ("{:>5}".format(total_before))
        + " | " + ("{:>5}".format(total_after))
        + " | " + ("{:>5}".format("-"))
        + " | " + ("{:>4}".format("-"))
        + " | " + ("{:>5}".format(str(pct_global) + "%"))
    )
    print()
    print("Cible : >= 40% vérifié + probable")
    print("Atteint : " + ("OUI ✓" if pct_global >= 40 else "NON ✗"))

    # Dumper les résultats détaillés
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump({"results": results, "global_quality_pct": pct_global}, f, ensure_ascii=False, indent=2)
    print("\nDétails écrits dans : " + args.output)


if __name__ == "__main__":
    main()
