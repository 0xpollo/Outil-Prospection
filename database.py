"""Gestion de la base de données SQLite pour l'historique des recherches.

Contient aussi la queue de jobs : un job est une recherche en cours d'exécution
par un worker séparé (cf. worker.py). L'UI Streamlit crée des jobs et lit leur
status pour afficher la progression — sans rien exécuter elle-même côté serveur.
"""

import sqlite3
import json
import os
import socket
from pathlib import Path

DB_PATH = Path(__file__).parent / "prospection.db"


def get_connection() -> sqlite3.Connection:
    """Retourne une connexion SQLite avec row_factory."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Crée les tables si elles n'existent pas."""
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activite TEXT NOT NULL,
            zone TEXT NOT NULL,
            nb_resultats INTEGER NOT NULL DEFAULT 0,
            date_recherche TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            parametres TEXT
        );

        -- Queue de jobs exécutés par worker.py.
        -- status : 'pending' | 'running' | 'done' | 'failed' | 'cancelled'
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

        CREATE TABLE IF NOT EXISTS entreprises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT NOT NULL,
            adresse TEXT NOT NULL DEFAULT '',
            telephone TEXT DEFAULT '',
            site_web TEXT DEFAULT '',
            emails TEXT DEFAULT '',
            note TEXT DEFAULT '',
            nb_avis INTEGER DEFAULT 0,
            score INTEGER DEFAULT 0,
            siren TEXT DEFAULT '',
            dirigeant_prenom TEXT DEFAULT '',
            dirigeant_nom TEXT DEFAULT '',
            dirigeant_qualite TEXT DEFAULT '',
            email_dirigeant TEXT DEFAULT '',
            email_dirigeant_confidence TEXT DEFAULT '',
            email_status TEXT DEFAULT '',
            email_confidence TEXT DEFAULT '',
            contenu_site TEXT DEFAULT '',
            date_creation TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            date_mise_a_jour TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_entreprise_unique
            ON entreprises(nom, adresse);

        CREATE TABLE IF NOT EXISTS recherche_entreprises (
            recherche_id INTEGER REFERENCES searches(id) ON DELETE CASCADE,
            entreprise_id INTEGER REFERENCES entreprises(id) ON DELETE CASCADE,
            deja_connue INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (recherche_id, entreprise_id)
        );
    """)

    # Migration : ajouter les colonnes dirigeant si la base existait déjà
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(entreprises)").fetchall()}
    for col in ("siren", "dirigeant_prenom", "dirigeant_nom", "dirigeant_qualite",
                "email_dirigeant", "email_dirigeant_confidence",
                "email_status", "email_confidence", "contenu_site"):
        if col not in existing_cols:
            conn.execute("ALTER TABLE entreprises ADD COLUMN {} TEXT DEFAULT ''".format(col))

    # Migration : ajouter stats_json au cas où la table jobs existait déjà
    existing_jobs_cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if existing_jobs_cols and "stats_json" not in existing_jobs_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN stats_json TEXT NOT NULL DEFAULT ''")

    conn.commit()
    conn.close()


def save_search(activite: str, zone: str, parametres: dict,
                entreprises: list[dict]) -> int:
    """
    Sauvegarde une recherche et ses résultats.
    Retourne l'ID de la recherche.
    Ajoute 'deja_connue' (bool) à chaque dict d'entreprise.
    """
    conn = get_connection()

    cursor = conn.execute(
        "INSERT INTO searches (activite, zone, nb_resultats, parametres) VALUES (?, ?, ?, ?)",
        (activite, zone, len(entreprises), json.dumps(parametres, ensure_ascii=False)),
    )
    search_id = cursor.lastrowid

    for e in entreprises:
        # Vérifier si l'entreprise existe déjà
        existing = conn.execute(
            "SELECT id FROM entreprises WHERE nom = ? AND adresse = ?",
            (e.get("nom", ""), e.get("adresse", "")),
        ).fetchone()

        deja_connue = existing is not None

        # Insérer ou mettre à jour
        conn.execute(
            """
            INSERT INTO entreprises (nom, adresse, telephone, site_web, emails, note, nb_avis, score,
                                     siren, dirigeant_prenom, dirigeant_nom, dirigeant_qualite,
                                     email_dirigeant, email_dirigeant_confidence,
                                     email_status, email_confidence, contenu_site)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(nom, adresse) DO UPDATE SET
                telephone = excluded.telephone,
                site_web = excluded.site_web,
                emails = excluded.emails,
                note = excluded.note,
                nb_avis = excluded.nb_avis,
                score = excluded.score,
                siren = CASE WHEN excluded.siren != '' THEN excluded.siren ELSE siren END,
                dirigeant_prenom = CASE WHEN excluded.dirigeant_prenom != '' THEN excluded.dirigeant_prenom ELSE dirigeant_prenom END,
                dirigeant_nom = CASE WHEN excluded.dirigeant_nom != '' THEN excluded.dirigeant_nom ELSE dirigeant_nom END,
                dirigeant_qualite = CASE WHEN excluded.dirigeant_qualite != '' THEN excluded.dirigeant_qualite ELSE dirigeant_qualite END,
                email_dirigeant = CASE WHEN excluded.email_dirigeant != '' THEN excluded.email_dirigeant ELSE email_dirigeant END,
                email_dirigeant_confidence = CASE WHEN excluded.email_dirigeant_confidence != '' THEN excluded.email_dirigeant_confidence ELSE email_dirigeant_confidence END,
                email_status = CASE WHEN excluded.email_status != '' THEN excluded.email_status ELSE email_status END,
                email_confidence = CASE WHEN excluded.email_confidence != '' THEN excluded.email_confidence ELSE email_confidence END,
                contenu_site = CASE WHEN excluded.contenu_site != '' THEN excluded.contenu_site ELSE contenu_site END,
                date_mise_a_jour = datetime('now', 'localtime')
            """,
            (
                e.get("nom", ""), e.get("adresse", ""),
                e.get("telephone", ""), e.get("site_web", ""),
                e.get("emails", ""), e.get("note", ""),
                e.get("nb_avis", 0), e.get("score", 0),
                e.get("siren", ""), e.get("dirigeant_prenom", ""),
                e.get("dirigeant_nom", ""), e.get("dirigeant_qualite", ""),
                e.get("email_dirigeant", ""), e.get("email_dirigeant_confidence", ""),
                e.get("email_status", ""), e.get("email_confidence", ""),
                e.get("contenu_site", ""),
            ),
        )

        # Récupérer l'ID de l'entreprise
        row = conn.execute(
            "SELECT id FROM entreprises WHERE nom = ? AND adresse = ?",
            (e.get("nom", ""), e.get("adresse", "")),
        ).fetchone()

        if row:
            conn.execute(
                "INSERT OR IGNORE INTO recherche_entreprises (recherche_id, entreprise_id, deja_connue) "
                "VALUES (?, ?, ?)",
                (search_id, row["id"], int(deja_connue)),
            )

        e["deja_connue"] = deja_connue

    conn.commit()
    conn.close()
    return search_id


def get_searches() -> list[dict]:
    """Retourne toutes les recherches, les plus récentes en premier."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM searches ORDER BY date_recherche DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_search_results(search_id: int) -> list[dict]:
    """Retourne les entreprises d'une recherche donnée."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT e.*, re.deja_connue
        FROM entreprises e
        JOIN recherche_entreprises re ON re.entreprise_id = e.id
        WHERE re.recherche_id = ?
        ORDER BY e.score DESC
        """,
        (search_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_entreprises(entreprises):
    """Met à jour les emails, scores, validation SMTP et contenu site d'entreprises existantes."""
    conn = get_connection()
    for e in entreprises:
        conn.execute(
            """
            UPDATE entreprises SET emails = ?, score = ?,
                email_status = ?, email_confidence = ?,
                contenu_site = CASE WHEN ? != '' THEN ? ELSE contenu_site END,
                date_mise_a_jour = datetime('now', 'localtime')
            WHERE nom = ? AND adresse = ?
            """,
            (e.get("emails", ""), e.get("score", 0),
             e.get("email_status", ""), e.get("email_confidence", ""),
             e.get("contenu_site", ""), e.get("contenu_site", ""),
             e.get("nom", ""), e.get("adresse", "")),
        )
    conn.commit()
    conn.close()


def delete_search(search_id: int):
    """Supprime une recherche et ses liens (les entreprises orphelines restent)."""
    conn = get_connection()
    conn.execute("DELETE FROM recherche_entreprises WHERE recherche_id = ?", (search_id,))
    conn.execute("DELETE FROM searches WHERE id = ?", (search_id,))
    # Nettoyer les entreprises qui ne sont liées à aucune recherche
    conn.execute("""
        DELETE FROM entreprises WHERE id NOT IN (
            SELECT DISTINCT entreprise_id FROM recherche_entreprises
        )
    """)
    conn.commit()
    conn.close()


def delete_all_history():
    """Supprime tout l'historique."""
    conn = get_connection()
    conn.executescript("""
        DELETE FROM recherche_entreprises;
        DELETE FROM searches;
        DELETE FROM entreprises;
    """)
    conn.commit()
    conn.close()


# ============================================================================
# Queue de jobs (utilisée par l'UI pour créer, par worker.py pour exécuter)
# ============================================================================

def _worker_id():
    """Identifiant unique du worker (host + pid)."""
    return socket.gethostname() + "/" + str(os.getpid())


def create_job(activite: str, zone: str, params: dict) -> int:
    """Crée un job pending dans la queue. Retourne son id."""
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO jobs (activite, zone, params_json) VALUES (?, ?, ?)",
        (activite, zone, json.dumps(params, ensure_ascii=False)),
    )
    job_id = cur.lastrowid
    conn.commit()
    conn.close()
    return job_id


def get_job(job_id: int):
    """Retourne un job (dict) ou None."""
    conn = get_connection()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_jobs(limit: int = 50, status_in: list = None) -> list:
    """Liste les jobs, plus récents en premier. Filtre optionnel par status."""
    conn = get_connection()
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
    conn.close()
    return [dict(r) for r in rows]


def claim_next_job() -> dict:
    """Le worker appelle ça en boucle : récupère le 1er job pending et le passe
    en 'running' atomiquement. Retourne le job claimé ou None s'il n'y en a pas.
    """
    conn = get_connection()
    try:
        # Transaction immediate pour éviter les double-claim
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


def update_job_progress(job_id: int, progress: float, message: str = ""):
    """Met à jour la progression (0.0–1.0) et le message d'un job running."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET progress = ?, message = ? WHERE id = ?",
        (max(0.0, min(1.0, float(progress))), message[:500], job_id),
    )
    conn.commit()
    conn.close()


def finish_job(job_id: int, status: str, search_id: int = None,
               results_count: int = 0, error: str = "", stats: dict = None):
    """Marque un job comme terminé (done / failed / cancelled)."""
    conn = get_connection()
    stats_json = json.dumps(stats, ensure_ascii=False) if stats else ""
    conn.execute(
        "UPDATE jobs SET status = ?, finished_at = datetime('now', 'localtime'), "
        "search_id = ?, results_count = ?, error = ?, stats_json = ? WHERE id = ?",
        (status, search_id, results_count, error[:1000], stats_json, job_id),
    )
    conn.commit()
    conn.close()


def cancel_job(job_id: int) -> bool:
    """Annule un job pending OU running. Retourne True si fait.

    Pour un job running, le worker sera redémarré par systemd et ne reprendra
    pas un job marqué cancelled. Pour un arrêt immédiat il faut aussi tuer le
    process worker (cf. UI : restart systemd outil-prospection-worker).
    """
    conn = get_connection()
    cur = conn.execute(
        "UPDATE jobs SET status = 'cancelled', "
        "finished_at = datetime('now', 'localtime'), "
        "error = 'arrêté par l''utilisateur' "
        "WHERE id = ? AND status IN ('pending', 'running')",
        (job_id,),
    )
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return changed > 0


def delete_job(job_id: int):
    """Supprime un job de la queue (uniquement si pas running)."""
    conn = get_connection()
    conn.execute(
        "DELETE FROM jobs WHERE id = ? AND status != 'running'", (job_id,),
    )
    conn.commit()
    conn.close()


def reap_stale_jobs(stale_minutes: int = 60):
    """Marque comme 'failed' les jobs running depuis trop longtemps (worker crashé).
    À appeler au démarrage du worker pour nettoyer un crash précédent.
    """
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET status = 'failed', "
        "finished_at = datetime('now', 'localtime'), "
        "error = 'worker stale (timeout)' "
        "WHERE status = 'running' "
        "AND (julianday('now', 'localtime') - julianday(started_at)) * 24 * 60 > ?",
        (stale_minutes,),
    )
    conn.commit()
    conn.close()


# Initialiser la base au premier import
init_db()
