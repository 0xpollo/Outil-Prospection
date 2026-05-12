"""Classification de l'hébergeur de mail d'un domaine.

Sortie : (provider, mx_type) où
  provider ∈ {ovh, ionos, m365, gworkspace, zoho, cleanmail, fastmail,
              amazon, proton, lws, no_mx, other}
  mx_type ∈ {discriminatif, opaque, no_mx, ''}

`discriminatif`  : le MX répond de façon fiable à RCPT TO (valid/invalid net)
`opaque`         : le MX répond `unknown` à tout RCPT, SMTP n'apporte aucun signal
`no_mx`          : le domaine n'a pas d'enregistrement MX
`''`             : statut indéterminé (erreur réseau, à retenter)

Heuristique :
1. DoH (Cloudflare) → liste des hostnames MX
2. Pattern-match contre une liste de providers connus
3. Si provider inconnu (`other`) → sonde bogus_xyz@domain via smtp_verifier
   - SMTP répond `invalid` (5xx net) → mx_type = discriminatif
   - sinon → mx_type = opaque

Cache mémoire par domaine (un run = un appel max par domaine).
"""

import logging
import re
import threading

import requests

import smtp_verifier

logger = logging.getLogger(__name__)

# Cache process-local, partagé entre threads
_CACHE = {}
_CACHE_LOCK = threading.Lock()

# Pattern de chaque provider connu, matché contre le hostname MX (sans
# trailing dot, lowercased). Ordre = priorité de match.
_PROVIDER_PATTERNS = [
    # discriminatifs : SMTP RCPT TO fiable
    ("m365", "discriminatif", re.compile(r"\.protection\.outlook\.com$")),
    ("gworkspace", "discriminatif",
     re.compile(r"(^|\.)aspmx[0-9]*\.l\.google\.com$|(^|\.)aspmx\.googlemail\.com$|"
                r"(^|\.)alt[0-9]+\.aspmx\.l\.google\.com$|"
                r"(^|\.)gmail-smtp-in\.l\.google\.com$|"
                r"(^|\.)aspmx-v[0-9]+\.googlemail\.com$")),
    ("zoho", "discriminatif", re.compile(r"\.zoho(mail|-mail)?\.(com|eu)$|"
                                          r"\.zoho\.com$|\.zoho\.eu$|"
                                          r"mx\.zohomail\.(com|eu)$")),
    ("fastmail", "discriminatif", re.compile(r"\.messagingengine\.com$")),
    ("proton", "discriminatif", re.compile(r"\.protonmail\.ch$|"
                                            r"\.mail\.protonmail\.ch$")),
    ("amazon", "discriminatif", re.compile(r"\.amazonses\.com$|"
                                            r"\.awsapps\.com$")),

    # opaques connus : SMTP répond unknown à tout RCPT
    ("ovh", "opaque", re.compile(r"(^|\.)mx[0-9a-z]*\.(ovh|kimsufi|sys)\.net$|"
                                  r"(^|\.)mx[0-9a-z]*\.mail\.ovh\.net$|"
                                  r"(^|\.)ssl0\.ovh\.net$|"
                                  r"(^|\.)mxplan\.com$")),
    ("ionos", "opaque", re.compile(r"\.(ionos|1and1)\.(fr|com|de|es|it|co\.uk)$|"
                                    r"\.kundenserver\.de$|"
                                    r"\.mx\.kundenserver\.de$")),
    ("cleanmail", "opaque", re.compile(r"\.cleanmail\.eu$")),
    ("lws", "opaque", re.compile(r"\.lws-mail\.com$|\.lwspanel\.com$")),
]


def _doh_mx(domain, timeout=4):
    """Récupère les hostnames MX d'un domaine via Cloudflare DoH.
    Retourne une liste de hostnames triés par priorité (low priority = first).
    [] si pas de MX, None si erreur réseau (à distinguer)."""
    try:
        resp = requests.get(
            "https://cloudflare-dns.com/dns-query",
            params={"name": domain, "type": "MX"},
            headers={"Accept": "application/dns-json"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None

    # Status 0 = NOERROR ; 3 = NXDOMAIN (pas de MX)
    status = data.get("Status", -1)
    answers = data.get("Answer") or []
    if not answers:
        if status == 0:
            return []  # NOERROR mais aucune réponse → pas de MX
        if status == 3:
            return []  # NXDOMAIN
        return None  # autre = ambigu

    entries = []
    for ans in answers:
        if ans.get("type") != 15:  # 15 = MX
            continue
        raw = (ans.get("data") or "").strip()
        # Format : "10 mx1.ovh.net."
        parts = raw.split(None, 1)
        if len(parts) == 2:
            try:
                prio = int(parts[0])
            except ValueError:
                prio = 999
            host = parts[1].rstrip(".").lower()
            entries.append((prio, host))
        else:
            entries.append((999, raw.rstrip(".").lower()))
    entries.sort(key=lambda x: x[0])
    return [h for _, h in entries]


def _match_provider(mx_hosts):
    """Cherche un match de provider dans la liste de hostnames MX.
    Retourne (provider, mx_type) ou (None, None) si rien ne matche."""
    for host in mx_hosts:
        for provider, mx_type, pattern in _PROVIDER_PATTERNS:
            if pattern.search(host):
                return provider, mx_type
    return None, None


def _probe_bogus(domain):
    """Sonde RCPT TO sur un email volontairement inexistant.
    Si le MX répond invalid (5xx net) → discriminatif.
    Sinon (catchall, unknown, error) → opaque.
    """
    bogus = "noexist-xyz-aaa-zzz-12345@" + domain
    status = smtp_verifier.verify_email(bogus)
    if status == "invalid":
        return "discriminatif"
    if status == "no_mx":
        return "no_mx"
    # valid / catchall / unknown / error → on classe opaque par prudence
    return "opaque"


def classify(domain):
    """Retourne (provider, mx_type) pour un domaine donné. Caché par run.

    En cas d'erreur réseau (DoH HS), retourne (provider='', mx_type='') —
    l'appelant doit traiter comme "indéterminé" et ne PAS générer de patterns.
    """
    if not domain:
        return ("", "")
    domain = domain.lower().strip()
    with _CACHE_LOCK:
        if domain in _CACHE:
            return _CACHE[domain]

    mx_hosts = _doh_mx(domain)
    if mx_hosts is None:
        # Erreur réseau, on ne cache pas pour retenter plus tard
        logger.warning("MX classify %s : DoH a échoué", domain)
        return ("", "")

    if not mx_hosts:
        result = ("no_mx", "no_mx")
        with _CACHE_LOCK:
            _CACHE[domain] = result
        logger.info("MX classify %s : no_mx", domain)
        return result

    provider, mx_type = _match_provider(mx_hosts)
    if provider is None:
        # Provider inconnu → sonde bogus pour décider opaque vs discriminatif
        decided = _probe_bogus(domain)
        if decided == "no_mx":
            result = ("no_mx", "no_mx")
        else:
            result = ("other", decided)
        logger.info(
            "MX classify %s : other (%s) — MX=%s — sonde=%s",
            domain, decided, mx_hosts[:2], decided,
        )
    else:
        result = (provider, mx_type)
        logger.info(
            "MX classify %s : %s/%s (MX=%s)",
            domain, provider, mx_type, mx_hosts[:2],
        )

    with _CACHE_LOCK:
        _CACHE[domain] = result
    return result


def reset_cache():
    """À appeler entre les runs."""
    with _CACHE_LOCK:
        _CACHE.clear()
