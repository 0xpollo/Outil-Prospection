"""Vérification SMTP d'emails via le service VPS Contabo (port 8000).

Le service effectue un SMTP RCPT TO réel sur le serveur MX du domaine.
Plus précis que les services tiers sur Microsoft 365 / Google Workspace
(la plupart des PME modernes). Sur OVH/IONOS, le serveur retourne souvent
"unknown" — pas de Debounce en fallback ici (volontairement exclu de la
porte) : ces emails sont classés "probable" et le pipeline continue.

Réponses du service :
- "valid"        → l'adresse existe (RCPT TO accepté)
- "invalid"      → l'adresse n'existe pas (5xx)
- "catchall"     → le domaine accepte tout email
- "unknown"      → le serveur ne répond pas aux RCPT (OVH/IONOS notamment)
- "no_mx"        → pas de MX, le domaine n'a pas d'email
- "invalid_format" → format invalide
"""

import logging
import requests

from config import VERIFIER_URL, VERIFIER_KEY

logger = logging.getLogger(__name__)

# Cache par run pour éviter de retester un même domaine ou email
_DOMAIN_STATUS_CACHE = {}  # domain → "no_mx" / "catchall" / "ok" / "unknown"
_EMAIL_RESULT_CACHE = {}   # email → status
_AVAILABLE_CACHE = None    # None / True / False (résultat du 1er health-check)

# Timeout des appels au service VPS.
# Le user-spec était 8s, mais en pratique /catchall prend ~8s côté serveur
# (RCPT TO réel sur le MX du domaine cible) → on bumpe à 20s pour /catchall et
# /verify, qui sont les routes lentes. /health reste sur 5s (route triviale).
_TIMEOUT = 20
_HEALTH_TIMEOUT = 5


def is_available():
    """Service VPS configuré et joignable ? Caché par run (1 seul health-check).

    Important : un blip réseau passager peut faire échouer le /health alors
    que le service répond aux /verify normalement. Le cache évite ce flake.
    Si on veut re-tester (entre runs), appeler reset_cache().
    """
    global _AVAILABLE_CACHE
    if _AVAILABLE_CACHE is not None:
        return _AVAILABLE_CACHE
    if not VERIFIER_URL or not VERIFIER_KEY:
        _AVAILABLE_CACHE = False
        return False
    # 2 essais avec petit délai — robuste à un blip réseau ponctuel
    for attempt in range(2):
        try:
            resp = requests.get(VERIFIER_URL + "/health", timeout=_HEALTH_TIMEOUT)
            if resp.status_code == 200:
                _AVAILABLE_CACHE = True
                return True
        except Exception:
            pass
        if attempt == 0:
            import time as _t
            _t.sleep(0.5)
    _AVAILABLE_CACHE = False
    return False


def check_domain(domain):
    """Statut d'un domaine (catchall + MX). "no_mx" / "catchall" / "ok" / "unknown".
    Caché par run.

    Retry intégré sur "no_mx" : le DNS côté service peut occasionnellement
    rater une résolution MX et renvoyer no_mx pour un domaine qui en a un.
    On retente 1 fois après 1s avant de conclure.
    """
    if not domain:
        return "unknown"
    domain = domain.lower().strip()
    if domain in _DOMAIN_STATUS_CACHE:
        return _DOMAIN_STATUS_CACHE[domain]
    if not VERIFIER_URL or not VERIFIER_KEY:
        return "unknown"

    last_status = "unknown"
    for attempt in range(2):
        try:
            resp = requests.get(
                VERIFIER_URL + "/catchall",
                params={"domain": domain, "key": VERIFIER_KEY},
                timeout=_TIMEOUT,
            )
            if resp.status_code != 200:
                last_status = "unknown"
                continue
            data = resp.json()
            status = data.get("status", "")
            if status == "no_mx":
                last_status = "no_mx"
                # Retry 1 fois (blip DNS possible côté serveur)
                if attempt == 0:
                    import time as _t
                    _t.sleep(1.0)
                    continue
            if data.get("catchall") is True:
                _DOMAIN_STATUS_CACHE[domain] = "catchall"
                return "catchall"
            if status != "no_mx":
                _DOMAIN_STATUS_CACHE[domain] = "ok"
                return "ok"
        except Exception as e:
            logger.debug("SMTP check_domain %s : %s", domain, e)
            last_status = "unknown"
    _DOMAIN_STATUS_CACHE[domain] = last_status
    return last_status


def verify_email(email):
    """Vérifier un email via le service.
    Retourne : "valid", "invalid", "catchall", "unknown", "no_mx", "error".
    Caché par run.
    """
    if not email or "@" not in email:
        return "error"
    email = email.lower().strip()
    if email in _EMAIL_RESULT_CACHE:
        return _EMAIL_RESULT_CACHE[email]
    if not VERIFIER_URL or not VERIFIER_KEY:
        return "error"

    domain = email.split("@", 1)[1]
    # Court-circuiter si on connaît déjà le domaine
    cached = _DOMAIN_STATUS_CACHE.get(domain)
    if cached == "no_mx":
        _EMAIL_RESULT_CACHE[email] = "no_mx"
        return "no_mx"
    if cached == "catchall":
        _EMAIL_RESULT_CACHE[email] = "catchall"
        return "catchall"

    try:
        resp = requests.get(
            VERIFIER_URL + "/verify",
            params={"email": email, "key": VERIFIER_KEY},
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.debug("SMTP verify %s : HTTP %s", email, resp.status_code)
            return "error"
        data = resp.json()
        status = data.get("status", "unknown")
        # Mémoriser le statut domaine
        if status == "no_mx":
            _DOMAIN_STATUS_CACHE[domain] = "no_mx"
        elif status == "catchall":
            _DOMAIN_STATUS_CACHE[domain] = "catchall"
        elif data.get("catchall") is False and status in ("valid", "invalid"):
            _DOMAIN_STATUS_CACHE[domain] = "ok"
        _EMAIL_RESULT_CACHE[email] = status
        return status
    except requests.exceptions.Timeout:
        logger.debug("SMTP verify %s : timeout", email)
        return "error"
    except Exception as e:
        logger.debug("SMTP verify %s : %s", email, e)
        return "error"


def reset_cache():
    """À appeler entre les runs (vide les caches en mémoire)."""
    global _AVAILABLE_CACHE
    _DOMAIN_STATUS_CACHE.clear()
    _EMAIL_RESULT_CACHE.clear()
    _AVAILABLE_CACHE = None
