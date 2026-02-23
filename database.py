"""Gestion de la base de données SQLite pour l'historique des recherches."""

import sqlite3
import json
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
            INSERT INTO entreprises (nom, adresse, telephone, site_web, emails, note, nb_avis, score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(nom, adresse) DO UPDATE SET
                telephone = excluded.telephone,
                site_web = excluded.site_web,
                emails = excluded.emails,
                note = excluded.note,
                nb_avis = excluded.nb_avis,
                score = excluded.score,
                date_mise_a_jour = datetime('now', 'localtime')
            """,
            (
                e.get("nom", ""), e.get("adresse", ""),
                e.get("telephone", ""), e.get("site_web", ""),
                e.get("emails", ""), e.get("note", ""),
                e.get("nb_avis", 0), e.get("score", 0),
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
    """Met à jour les emails et scores d'entreprises existantes."""
    conn = get_connection()
    for e in entreprises:
        conn.execute(
            """
            UPDATE entreprises SET emails = ?, score = ?,
                date_mise_a_jour = datetime('now', 'localtime')
            WHERE nom = ? AND adresse = ?
            """,
            (e.get("emails", ""), e.get("score", 0),
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


# Initialiser la base au premier import
init_db()
