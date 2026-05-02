"""Re-valide tous les emails de la DB via SMTP RCPT TO.

Couvre 2 colonnes :
  - emails (scrapé site)        -> écrit email_status + email_confidence
  - email_dirigeant             -> écrit email_dirigeant_confidence (FR labels)

Filtre par défaut : statut vide (jamais testé) ou "public" (perso jamais testé).
Avec --retry-unknown : ajoute aussi les "unknown" (MX OVH/IONOS ambigus).

Pour les domaines perso (gmail/outlook/...) : on teste quand même, mais comme
le MX accept-all rend le résultat peu fiable, on garde status="public" sauf
si le serveur dit "no_mx" (cas où l'email est vraiment cassé).

Usage :
  python3 revalidate_all_emails.py [--search-id N] [--retry-unknown]
                                   [--include-perso] [--limit M] [--dry-run]
"""

import argparse
import sqlite3
import sys
import time

import smtp_verifier
from email_finder import PUBLIC_EMAIL_DOMAINS


DB_PATH = "/opt/outil-prospection/prospection.db"

# Pour la colonne "emails" (sites pro) : SMTP status -> (email_status, email_confidence)
SCRAPED_MAP = {
    "valid":    ("valid",    "high"),
    "catchall": ("catchall", "medium"),
    "invalid":  ("invalid",  "none"),
    "no_mx":    ("no_mx",    "none"),
    "unknown":  ("unknown",  "low"),
}
# Pour email_dirigeant : SMTP status -> confiance (FR)
DIRIGEANT_MAP = {
    "valid":    "vérifié",
    "catchall": "probable",
    "unknown":  "probable",   # OVH/IONOS spec
    "invalid":  None,         # email faux : effacer
    "no_mx":    None,         # pas de MX : effacer
}


def revalidate_scraped(conn, args):
    statuses = [""]
    if args.retry_unknown:
        statuses.append("unknown")

    where = ["e.emails != ''",
             "(COALESCE(e.email_status, '') IN (%s))" % ",".join(["?"] * len(statuses))]
    params = list(statuses)
    if args.search_id:
        where.append("re.recherche_id = ?")
        params.append(args.search_id)

    sql = """
        SELECT DISTINCT e.id, e.nom, e.emails, e.email_status
        FROM entreprises e
        LEFT JOIN recherche_entreprises re ON re.entreprise_id = e.id
        WHERE """ + " AND ".join(where) + """
        ORDER BY e.id
    """
    if args.limit:
        sql += " LIMIT %d" % args.limit

    rows = list(conn.execute(sql, params))
    # Filtrer les domaines perso AVANT (jamais tester : Gmail/Outlook accept-all)
    rows_perso = [r for r in rows
                  if "@" in r["emails"]
                  and r["emails"].split("@", 1)[1].lower() in PUBLIC_EMAIL_DOMAINS]
    rows = [r for r in rows if r not in rows_perso]
    print("=== EMAILS SITE  : %d à tester (skip perso : %d) ===" %
          (len(rows), len(rows_perso)))
    # Aligner le statut DB des perso à "public" (au cas où ils étaient "")
    for r in rows_perso:
        if r["email_status"] != "public" and not args.dry_run:
            conn.execute(
                "UPDATE entreprises SET email_status = 'public', "
                "email_confidence = 'medium' WHERE id = ?",
                (r["id"],),
            )
    if not args.dry_run:
        conn.commit()

    stats = {"valid": 0, "catchall": 0, "invalid": 0, "no_mx": 0,
             "unknown": 0, "error": 0}
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        email = r["emails"]
        try:
            raw = smtp_verifier.verify_email(email)
        except Exception as e:
            raw = "error"
            print("  [%d] %s: ERROR %s" % (i, email, e))

        stats[raw] = stats.get(raw, 0) + 1
        mapping = SCRAPED_MAP.get(raw)
        if mapping is None:
            continue  # error -> garder ancien
        new_status, new_conf = mapping

        if not args.dry_run:
            conn.execute(
                "UPDATE entreprises SET email_status = ?, email_confidence = ? "
                "WHERE id = ?",
                (new_status, new_conf, r["id"]),
            )

        if i % 20 == 0 or i == len(rows):
            conn.commit()
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed else 0
            eta = (len(rows) - i) / rate if rate else 0
            print("  [%d/%d]  %ds  %.1f/s  ETA %ds  | %s -> %s" %
                  (i, len(rows), int(elapsed), rate, int(eta), email, raw))

    conn.commit()
    print("  Résultats SMTP bruts :")
    for k, v in sorted(stats.items()):
        print("    %-22s : %d" % (k, v))
    return time.time() - t0


def revalidate_dirigeants(conn, args):
    where = ["e.email_dirigeant != ''", "COALESCE(e.email_dirigeant_confidence, '') = ''"]
    params = []
    if args.search_id:
        where.append("re.recherche_id = ?")
        params.append(args.search_id)

    sql = """
        SELECT DISTINCT e.id, e.nom, e.email_dirigeant
        FROM entreprises e
        LEFT JOIN recherche_entreprises re ON re.entreprise_id = e.id
        WHERE """ + " AND ".join(where) + """
        ORDER BY e.id
    """
    if args.limit:
        sql += " LIMIT %d" % args.limit

    rows = list(conn.execute(sql, params))
    print("=== EMAILS DIRIGEANT : %d à tester ===" % len(rows))

    stats = {"vérifié": 0, "probable": 0, "incertain": 0,
             "supprimé": 0, "erreur": 0}
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        email = r["email_dirigeant"]
        try:
            raw = smtp_verifier.verify_email(email)
        except Exception as e:
            raw = "error"
            print("  [%d] %s: ERROR %s" % (i, email, e))

        new_conf = DIRIGEANT_MAP.get(raw, "incertain")
        if new_conf is None:
            stats["supprimé"] += 1
            if not args.dry_run:
                conn.execute(
                    "UPDATE entreprises SET email_dirigeant = '', "
                    "email_dirigeant_confidence = '' WHERE id = ?",
                    (r["id"],),
                )
        elif raw == "error":
            stats["erreur"] += 1
        else:
            stats[new_conf] += 1
            if not args.dry_run:
                conn.execute(
                    "UPDATE entreprises SET email_dirigeant_confidence = ? "
                    "WHERE id = ?",
                    (new_conf, r["id"]),
                )

        if i % 20 == 0 or i == len(rows):
            conn.commit()
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed else 0
            eta = (len(rows) - i) / rate if rate else 0
            print("  [%d/%d]  %ds  %.1f/s  ETA %ds  | %s -> %s" %
                  (i, len(rows), int(elapsed), rate, int(eta), email, raw))

    conn.commit()
    print("  Résultats :")
    for k, v in stats.items():
        print("    %-12s : %d" % (k, v))
    return time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--search-id", type=int)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--retry-unknown", action="store_true",
                    help="Re-teste aussi les status=unknown")
    ap.add_argument("--skip-scraped", action="store_true",
                    help="Ne pas tester les emails scrapés")
    ap.add_argument("--skip-dirigeants", action="store_true",
                    help="Ne pas tester les emails dirigeants")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not smtp_verifier.is_available():
        print("ERREUR : service SMTP VPS indisponible.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    total_t = 0
    if not args.skip_scraped:
        total_t += revalidate_scraped(conn, args)
        print()
    if not args.skip_dirigeants:
        total_t += revalidate_dirigeants(conn, args)

    conn.close()
    print()
    print("Durée totale : %.0f s" % total_t)


if __name__ == "__main__":
    main()
