"""Gestion de la base de données SQLite — schéma v2 (mai 2026).

Refonte cut-net : la table `entreprises` ne contient plus que les champs
scalaires (nom, adresse, dirigeant, mx_*, tier…). Les emails vivent dans une
table dédiée `entreprise_emails` (1 entreprise → N emails), chacune avec sa
source, son statut SMTP, son rang destinataire.

Chaque entreprise expose en mémoire :
  entreprise["emails"] : list[dict]  où chaque dict a les clés
    email, source ('published'|'pattern'|'generic'),
    source_url, smtp_status, is_public_domain (bool), destinataire_rank (1..7)

L'ancien schéma (colonnes `email_dirigeant`, `emails_strategiques`…) est
détecté au démarrage et la base entière est recréée à zéro (la sauvegarde
préalable est de la responsabilité de l'utilisateur — cf. CLAUDE.md).
"""

import json
import os
import socket
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "prospection.db"

SCHEMA_VERSION = 2


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activite TEXT NOT NULL,
    zone TEXT NOT NULL,
    nb_resultats INTEGER NOT NULL DEFAULT 0,
    date_recherche TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    parametres TEXT
);

CREATE TABLE IF NOT EXISTS entreprises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT NOT NULL,
    adresse TEXT NOT NULL DEFAULT '',
    telephone TEXT DEFAULT '',
    site_web TEXT DEFAULT '',
    note TEXT DEFAULT '',
    nb_avis INTEGER DEFAULT 0,
    score INTEGER DEFAULT 0,
    siren TEXT DEFAULT '',
    dirigeant_prenom TEXT DEFAULT '',
    dirigeant_nom TEXT DEFAULT '',
    dirigeant_qualite TEXT DEFAULT '',
    mx_provider TEXT DEFAULT '',
    mx_type TEXT DEFAULT '',
    tier TEXT DEFAULT '',
    contenu_site TEXT DEFAULT '',
    date_creation TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    date_mise_a_jour TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_entreprise_unique
    ON entreprises(nom, adresse);

CREATE TABLE IF NOT EXISTS entreprise_emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entreprise_id INTEGER NOT NULL REFERENCES entreprises(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'published',
    source_url TEXT DEFAULT '',
    smtp_status TEXT DEFAULT '',
    is_public_domain INTEGER NOT NULL DEFAULT 0,
    destinataire_rank INTEGER NOT NULL DEFAULT 4,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE(entreprise_id, email)
);

CREATE INDEX IF NOT EXISTS idx_emails_entreprise
    ON entreprise_emails(entreprise_id);

CREATE TABLE IF NOT EXISTS recherche_entreprises (
    recherche_id INTEGER REFERENCES searches(id) ON DELETE CASCADE,
    entreprise_id INTEGER REFERENCES entreprises(id) ON DELETE CASCADE,
    deja_connue INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (recherche_id, entreprise_id)
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activite TEXT NOT NULL,
    zone TEXT NOT NULL,
    params_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    progress REAL NOT NULL DEFAULT 0.0,
    message TEXT NOT NULL DEFAULT '',
    results_count INTEGER NOT NULL DEFAULT 0,
    search_id INTEGER REFERENCES searches(id) ON DELETE SET NULL,
    error TEXT NOT NULL DEFAULT '',
    worker_id TEXT NOT NULL DEFAULT '',
    stats_json TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
"""


def get_connection():
    """Retourne une connexion SQLite avec row_factory et FK actives."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _detect_old_schema(conn):
    """True si la DB existante est l'ancien schéma (v1) à reconstruire."""
    tables = {
        r[0] for r in
        conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if not tables:
        return False  # base vierge, on est en v2 directement
    if "entreprise_emails" in tables and "schema_meta" in tables:
        # v2 déjà en place, vérifier la version
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone()
        if row and row[0] == str(SCHEMA_VERSION):
            return False
        return True
    # Tables anciennes présentes mais pas la v2 → ancien schéma
    if "entreprises" in tables:
        return True
    return False


def _wipe_db(conn):
    """Drop toutes les tables connues (FK off le temps de l'opération)."""
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript("""
        DROP INDEX IF EXISTS idx_entreprise_unique;
        DROP INDEX IF EXISTS idx_emails_entreprise;
        DROP INDEX IF EXISTS idx_jobs_status;
        DROP TABLE IF EXISTS entreprise_emails;
        DROP TABLE IF EXISTS recherche_entreprises;
        DROP TABLE IF EXISTS jobs;
        DROP TABLE IF EXISTS entreprises;
        DROP TABLE IF EXISTS searches;
        DROP TABLE IF EXISTS schema_meta;
    """)
    conn.execute("PRAGMA foreign_keys=ON")


def init_db():
    """Crée le schéma v2. Si une base v1 existe, la wipe d'abord."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        if _detect_old_schema(conn):
            _wipe_db(conn)
            conn.commit()
        conn.executescript(_SCHEMA_SQL)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Entreprises + emails
# ---------------------------------------------------------------------------

def _normalize_email_dict(item):
    """Garantit qu'un élément de `entreprise["emails"]` a la bonne forme.
    Accepte aussi une string brute (rétrocompat lors d'un import accidentel)."""
    if isinstance(item, str):
        return {
            "email": item.strip(),
            "source": "published",
            "source_url": "",
            "smtp_status": "",
            "is_public_domain": 0,
            "destinataire_rank": 4,
        }
    return {
        "email": (item.get("email") or "").strip(),
        "source": item.get("source") or "published",
        "source_url": item.get("source_url") or "",
        "smtp_status": item.get("smtp_status") or "",
        "is_public_domain": 1 if item.get("is_public_domain") else 0,
        "destinataire_rank": int(item.get("destinataire_rank") or 4),
    }


def _save_entreprise_emails(conn, entreprise_id, emails):
    """Remplace les emails d'une entreprise par la liste fournie.
    `emails` est une liste de dicts (ou une liste vide pour tout effacer).
    """
    conn.execute(
        "DELETE FROM entreprise_emails WHERE entreprise_id = ?",
        (entreprise_id,),
    )
    if not emails:
        return
    seen = set()
    for raw in emails:
        item = _normalize_email_dict(raw)
        email = item["email"].lower()
        if not email or email in seen:
            continue
        seen.add(email)
        conn.execute(
            """
            INSERT OR IGNORE INTO entreprise_emails
                (entreprise_id, email, source, source_url, smtp_status,
                 is_public_domain, destinataire_rank)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entreprise_id, email, item["source"], item["source_url"],
                item["smtp_status"], item["is_public_domain"],
                item["destinataire_rank"],
            ),
        )


def save_search(activite, zone, parametres, entreprises):
    """Sauvegarde une recherche et ses résultats. Retourne search_id.
    Marque `deja_connue` sur chaque dict d'entreprise."""
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO searches (activite, zone, nb_resultats, parametres) "
            "VALUES (?, ?, ?, ?)",
            (activite, zone, len(entreprises),
             json.dumps(parametres, ensure_ascii=False)),
        )
        search_id = cur.lastrowid

        for e in entreprises:
            nom = e.get("nom", "") or ""
            adresse = e.get("adresse", "") or ""
            existing = conn.execute(
                "SELECT id FROM entreprises WHERE nom = ? AND adresse = ?",
                (nom, adresse),
            ).fetchone()
            deja_connue = existing is not None

            conn.execute(
                """
                INSERT INTO entreprises
                    (nom, adresse, telephone, site_web, note, nb_avis, score,
                     siren, dirigeant_prenom, dirigeant_nom, dirigeant_qualite,
                     mx_provider, mx_type, tier, contenu_site)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(nom, adresse) DO UPDATE SET
                    telephone = excluded.telephone,
                    site_web = excluded.site_web,
                    note = excluded.note,
                    nb_avis = excluded.nb_avis,
                    score = excluded.score,
                    siren = CASE WHEN excluded.siren != '' THEN excluded.siren ELSE siren END,
                    dirigeant_prenom = CASE WHEN excluded.dirigeant_prenom != '' THEN excluded.dirigeant_prenom ELSE dirigeant_prenom END,
                    dirigeant_nom = CASE WHEN excluded.dirigeant_nom != '' THEN excluded.dirigeant_nom ELSE dirigeant_nom END,
                    dirigeant_qualite = CASE WHEN excluded.dirigeant_qualite != '' THEN excluded.dirigeant_qualite ELSE dirigeant_qualite END,
                    mx_provider = CASE WHEN excluded.mx_provider != '' THEN excluded.mx_provider ELSE mx_provider END,
                    mx_type = CASE WHEN excluded.mx_type != '' THEN excluded.mx_type ELSE mx_type END,
                    tier = excluded.tier,
                    contenu_site = CASE WHEN excluded.contenu_site != '' THEN excluded.contenu_site ELSE contenu_site END,
                    date_mise_a_jour = datetime('now', 'localtime')
                """,
                (
                    nom, adresse,
                    e.get("telephone", "") or "",
                    e.get("site_web", "") or "",
                    e.get("note", "") or "",
                    int(e.get("nb_avis") or 0),
                    int(e.get("score") or 0),
                    e.get("siren", "") or "",
                    e.get("dirigeant_prenom", "") or "",
                    e.get("dirigeant_nom", "") or "",
                    e.get("dirigeant_qualite", "") or "",
                    e.get("mx_provider", "") or "",
                    e.get("mx_type", "") or "",
                    e.get("tier", "") or "",
                    e.get("contenu_site", "") or "",
                ),
            )

            row = conn.execute(
                "SELECT id FROM entreprises WHERE nom = ? AND adresse = ?",
                (nom, adresse),
            ).fetchone()
            if not row:
                continue
            entreprise_id = row["id"]

            _save_entreprise_emails(conn, entreprise_id, e.get("emails") or [])

            conn.execute(
                "INSERT OR IGNORE INTO recherche_entreprises "
                "(recherche_id, entreprise_id, deja_connue) VALUES (?, ?, ?)",
                (search_id, entreprise_id, int(deja_connue)),
            )
            e["deja_connue"] = deja_connue

        conn.commit()
        return search_id
    finally:
        conn.close()


def get_searches():
    """Retourne toutes les recherches, plus récentes en premier."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM searches ORDER BY date_recherche DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _load_emails_for(conn, entreprise_ids):
    """Charge en bulk les emails de plusieurs entreprises.
    Retourne dict[entreprise_id -> list[email_dict]]."""
    if not entreprise_ids:
        return {}
    placeholders = ",".join("?" * len(entreprise_ids))
    rows = conn.execute(
        "SELECT entreprise_id, email, source, source_url, smtp_status, "
        "       is_public_domain, destinataire_rank "
        "FROM entreprise_emails "
        "WHERE entreprise_id IN (" + placeholders + ") "
        "ORDER BY destinataire_rank ASC, id ASC",
        list(entreprise_ids),
    ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["entreprise_id"], []).append({
            "email": r["email"],
            "source": r["source"],
            "source_url": r["source_url"] or "",
            "smtp_status": r["smtp_status"] or "",
            "is_public_domain": bool(r["is_public_domain"]),
            "destinataire_rank": r["destinataire_rank"],
        })
    return out


def get_search_results(search_id):
    """Retourne les entreprises d'une recherche, avec leurs emails attachés."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT e.*, re.deja_connue
            FROM entreprises e
            JOIN recherche_entreprises re ON re.entreprise_id = e.id
            WHERE re.recherche_id = ?
            ORDER BY e.score DESC, e.nom ASC
            """,
            (search_id,),
        ).fetchall()
        if not rows:
            return []
        entreprise_ids = [r["id"] for r in rows]
        emails_by_id = _load_emails_for(conn, entreprise_ids)
        results = []
        for r in rows:
            d = dict(r)
            d["emails"] = emails_by_id.get(r["id"], [])
            results.append(d)
        return results
    finally:
        conn.close()


def update_entreprises(entreprises):
    """Met à jour les champs principaux + remplace les emails de chaque entreprise.
    Identification par (nom, adresse)."""
    conn = get_connection()
    try:
        for e in entreprises:
            nom = e.get("nom", "") or ""
            adresse = e.get("adresse", "") or ""
            row = conn.execute(
                "SELECT id FROM entreprises WHERE nom = ? AND adresse = ?",
                (nom, adresse),
            ).fetchone()
            if not row:
                continue
            entreprise_id = row["id"]
            conn.execute(
                """
                UPDATE entreprises SET
                    score = ?, mx_provider = ?, mx_type = ?, tier = ?,
                    contenu_site = CASE WHEN ? != '' THEN ? ELSE contenu_site END,
                    date_mise_a_jour = datetime('now', 'localtime')
                WHERE id = ?
                """,
                (
                    int(e.get("score") or 0),
                    e.get("mx_provider", "") or "",
                    e.get("mx_type", "") or "",
                    e.get("tier", "") or "",
                    e.get("contenu_site", "") or "",
                    e.get("contenu_site", "") or "",
                    entreprise_id,
                ),
            )
            if "emails" in e:
                _save_entreprise_emails(conn, entreprise_id, e["emails"] or [])
        conn.commit()
    finally:
        conn.close()


def delete_search(search_id):
    """Supprime une recherche, ses liens, ET les entreprises orphelines.
    Les emails sont supprimés par cascade."""
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM recherche_entreprises WHERE recherche_id = ?",
            (search_id,),
        )
        conn.execute("DELETE FROM searches WHERE id = ?", (search_id,))
        conn.execute("""
            DELETE FROM entreprises WHERE id NOT IN (
                SELECT DISTINCT entreprise_id FROM recherche_entreprises
            )
        """)
        conn.commit()
    finally:
        conn.close()


def delete_all_history():
    """Supprime tout l'historique (emails cascadent)."""
    conn = get_connection()
    try:
        conn.executescript("""
            DELETE FROM recherche_entreprises;
            DELETE FROM searches;
            DELETE FROM entreprises;
        """)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Queue de jobs (inchangée fonctionnellement par rapport à la v1)
# ---------------------------------------------------------------------------

def _worker_id():
    return socket.gethostname() + "/" + str(os.getpid())


def create_job(activite, zone, params):
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO jobs (activite, zone, params_json) VALUES (?, ?, ?)",
            (activite, zone, json.dumps(params, ensure_ascii=False)),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_job(job_id):
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_jobs(limit=50, status_in=None):
    conn = get_connection()
    try:
        if status_in:
            placeholders = ",".join("?" * len(status_in))
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status IN (" + placeholders + ") "
                "ORDER BY id DESC LIMIT ?",
                list(status_in) + [limit],
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def claim_next_job():
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = 'pending' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return None
        conn.execute(
            "UPDATE jobs SET status = 'running', "
            "started_at = datetime('now', 'localtime'), "
            "worker_id = ? WHERE id = ?",
            (_worker_id(), row["id"]),
        )
        conn.commit()
        return dict(row)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def update_job_progress(job_id, progress, message=""):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE jobs SET progress = ?, message = ? WHERE id = ?",
            (max(0.0, min(1.0, float(progress))), (message or "")[:500], job_id),
        )
        conn.commit()
    finally:
        conn.close()


def finish_job(job_id, status, search_id=None, results_count=0,
               error="", stats=None):
    conn = get_connection()
    try:
        stats_json = json.dumps(stats, ensure_ascii=False) if stats else ""
        conn.execute(
            "UPDATE jobs SET status = ?, finished_at = datetime('now', 'localtime'), "
            "search_id = ?, results_count = ?, error = ?, stats_json = ? WHERE id = ?",
            (status, search_id, results_count, (error or "")[:1000],
             stats_json, job_id),
        )
        conn.commit()
    finally:
        conn.close()


def cancel_job(job_id):
    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE jobs SET status = 'cancelled', "
            "finished_at = datetime('now', 'localtime'), "
            "error = 'arrêté par l''utilisateur' "
            "WHERE id = ? AND status IN ('pending', 'running')",
            (job_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_job(job_id):
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM jobs WHERE id = ? AND status != 'running'",
            (job_id,),
        )
        conn.commit()
    finally:
        conn.close()


def reap_stale_jobs(stale_minutes=60):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE jobs SET status = 'failed', "
            "finished_at = datetime('now', 'localtime'), "
            "error = 'worker stale (timeout)' "
            "WHERE status = 'running' "
            "AND (julianday('now', 'localtime') - julianday(started_at)) * 24 * 60 > ?",
            (stale_minutes,),
        )
        conn.commit()
    finally:
        conn.close()


# Initialiser la base au premier import
init_db()
