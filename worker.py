"""Worker process : exécute les jobs de la queue (table `jobs`).

Tourne en boucle infinie : poll la DB toutes les 2 s, claim un job pending,
exécute le pipeline complet, écrit la progression en temps réel.

Lancer :
    python3 worker.py

Ou via systemd (cf. déploiement VPS) :
    systemctl start outil-prospection-worker

Le worker peut être tué/redémarré à chaud — les jobs déjà running seront
reapés (marqués 'failed' avec error='worker stale') au prochain démarrage,
puis pourront être relancés manuellement depuis l'UI.
"""

import json
import logging
import signal
import sys
import time
import traceback

import database
from pipeline import run_search


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] worker: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)


_RUNNING = True


def _handle_signal(signum, frame):
    global _RUNNING
    logger.info("Signal %s reçu, arrêt en fin de job courant...", signum)
    _RUNNING = False


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# Throttle des updates DB pour ne pas saturer la lecture concurrente côté UI
_LAST_PROGRESS_TS = [0.0]
_LAST_PROGRESS_RATIO = [0.0]
_PROGRESS_MIN_INTERVAL = 1.0  # secondes
_PROGRESS_MIN_DELTA = 0.01    # 1 % minimum


def _make_progress_cb(job_id):
    """Renvoie un callback (msg, ratio) qui écrit dans la DB avec throttling."""
    def cb(msg, ratio):
        now = time.time()
        if (
            now - _LAST_PROGRESS_TS[0] < _PROGRESS_MIN_INTERVAL
            and abs(ratio - _LAST_PROGRESS_RATIO[0]) < _PROGRESS_MIN_DELTA
            and ratio not in (0.0, 1.0)
        ):
            return
        _LAST_PROGRESS_TS[0] = now
        _LAST_PROGRESS_RATIO[0] = ratio
        try:
            database.update_job_progress(job_id, ratio, msg)
        except Exception as e:
            logger.warning("update_job_progress %s : %s", job_id, e)
    return cb


def _execute_job(job):
    """Exécute un job claimé. Marque done/failed à la fin."""
    job_id = job["id"]
    try:
        params = json.loads(job["params_json"]) if job.get("params_json") else {}
    except json.JSONDecodeError:
        params = {}
    params["activite"] = job.get("activite", "")
    params["zone"] = job.get("zone", "")

    logger.info(
        "Job #%d : %s / %s (params %d clés)",
        job_id, params["activite"], params["zone"], len(params),
    )

    cb = _make_progress_cb(job_id)
    cb("Initialisation...", 0.0)

    try:
        result = run_search(params, progress_callback=cb)
    except KeyboardInterrupt:
        logger.warning("Job #%d interrompu (KeyboardInterrupt)", job_id)
        database.finish_job(job_id, "failed", error="interrompu")
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.exception("Job #%d échec : %s", job_id, e)
        database.finish_job(
            job_id, "failed",
            error=(str(e)[:300] + "\n" + tb[-700:])[-1000:],
        )
        return

    database.finish_job(
        job_id, "done",
        search_id=result.get("search_id"),
        results_count=result.get("count", 0),
        stats=result.get("stats"),
    )
    logger.info(
        "Job #%d terminé : search_id=%s, %d entreprise(s), stats=%s",
        job_id, result.get("search_id"), result.get("count", 0),
        result.get("stats"),
    )


def main():
    logger.info("Worker démarré (pid=%d)", __import__("os").getpid())
    # Nettoyer les jobs orphelins d'un crash précédent
    database.reap_stale_jobs(stale_minutes=60)

    while _RUNNING:
        try:
            job = database.claim_next_job()
        except Exception as e:
            logger.warning("claim_next_job erreur : %s", e)
            time.sleep(5)
            continue

        if job is None:
            time.sleep(2)
            continue

        try:
            _execute_job(job)
        except KeyboardInterrupt:
            break
        except Exception:
            # _execute_job gère normalement ses propres erreurs ; ce filet
            # rattrape un éventuel crash inattendu de la couche DB
            logger.exception("Erreur inattendue dans _execute_job")
            time.sleep(2)

    logger.info("Worker arrêté proprement")


if __name__ == "__main__":
    main()
