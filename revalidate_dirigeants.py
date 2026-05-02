"""Re-valide les emails dirigeants pour lesquels la confiance est vide en DB.

Cause racine : bug FR/EN entre `email_dirigeant_confiance` (Phase 2 advanced)
et la colonne DB `email_dirigeant_confidence`. Les emails ont été générés
par patterns mais la confiance SMTP n'a pas été persistée.

Ce script appelle directement smtp_verifier.verify_email pour chaque
email_dirigeant non testé et écrit le résultat (vérifié / probable /
incertain) dans la colonne email_dirigeant_confidence.

Usage : python3 revalidate_dirigeants.py [--search-id N] [--limit M] [--dry-run]
"""

import argparse
import sqlite3
import sys
import time

import smtp_verifier


DB_PATH = "/opt/outil-prospection/prospection.db"


def map_status(status):
    """SMTP status -> confiance FR. invalid -> None pour effacer l'email."""
    return {
        "valid": "vérifié",
        "catchall": "probable",
        "unknown": "probable",       # OVH/IONOS ambigu, spec
        "invalid": None,              # email faux : on l'efface
        "no_mx": None,                # domaine sans MX : email inutilisable
    }.get(status, "incertain")        # error / autre : best guess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--search-id", type=int, default=None,
                        help="Limiter à une recherche (sinon : toutes)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap sur le nombre d'emails à tester")
    parser.add_argument("--dry-run", action="store_true",
                        help="Ne rien écrire en DB")
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()

    if not smtp_verifier.is_available():
        print("ERREUR : service SMTP VPS indisponible.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    where = ["e.email_dirigeant != ''", "e.email_dirigeant_confidence = ''"]
    params = []
    if args.search_id is not None:
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
    print("Total emails à tester : %d" % len(rows))
    if args.dry_run:
        print("(dry-run : aucune écriture)")

    stats = {"vérifié": 0, "probable": 0, "incertain": 0,
             "supprimé": 0, "erreur": 0}
    t0 = time.time()

    for i, r in enumerate(rows, 1):
        email = r["email_dirigeant"]
        try:
            status = smtp_verifier.verify_email(email)
        except Exception as e:
            print("[%d/%d] %s  ERROR: %s" % (i, len(rows), email, e))
            stats["erreur"] += 1
            continue

        new_conf = map_status(status)
        if new_conf is None:
            # invalid / no_mx : effacer l'email (il est faux)
            label = "supprimé (%s)" % status
            if not args.dry_run:
                conn.execute(
                    "UPDATE entreprises SET email_dirigeant = '', "
                    "email_dirigeant_confidence = '' WHERE id = ?",
                    (r["id"],),
                )
            stats["supprimé"] += 1
        else:
            label = "%s (%s)" % (new_conf, status)
            if not args.dry_run:
                conn.execute(
                    "UPDATE entreprises SET email_dirigeant_confidence = ? "
                    "WHERE id = ?",
                    (new_conf, r["id"]),
                )
            stats[new_conf] += 1

        if i % 20 == 0 or i == len(rows):
            conn.commit()
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed else 0
            eta = (len(rows) - i) / rate if rate else 0
            print("[%d/%d]  %ds  %.1f/s  ETA %ds  | %s : %s -> %s" %
                  (i, len(rows), int(elapsed), rate, int(eta),
                   r["nom"][:30], email, label))
        # pas de sleep agressif : smtp_verifier cache déjà par domaine

    conn.commit()
    conn.close()

    print()
    print("=== Résultats ===")
    for k in ("vérifié", "probable", "incertain", "supprimé", "erreur"):
        print("  %-12s : %d" % (k, stats[k]))
    print("Durée totale : %.1f s" % (time.time() - t0))


if __name__ == "__main__":
    main()
