"""Génération et validation d'emails nominatifs à partir du dirigeant + domaine.

Étapes :
1. Extrait le domaine depuis le site web
2. Génère des patterns d'emails probables (prenom.nom@, p.nom@, etc.)
3. Vérifie que le domaine a des enregistrements MX (email configuré)
4. Si le port 25 est accessible : valide via SMTP RCPT TO + détecte catchall
5. Sinon (cas fréquent en France, port 25 bloqué par le FAI) : retourne
   le pattern le plus probable avec confiance réduite

Confidence :
- 'high'   : SMTP confirme que la boîte existe et le domaine n'est pas catchall
- 'medium' : MX existe et pattern standard (ou catchall, ou SMTP indisponible)
- 'low'    : pattern généré mais aucun signal positif
- 'none'   : pas de domaine exploitable
"""

import random
import re
import smtplib
import socket
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, quote_plus

import dns.resolver
import dns.exception
import requests

try:
    from config import VERIFIER_URL, VERIFIER_KEY
except ImportError:
    VERIFIER_URL = ""
    VERIFIER_KEY = ""


# Timeouts pour SMTP et DNS
SMTP_TIMEOUT = 4
DNS_TIMEOUT = 3

# Flag global : détecté au premier appel SMTP qui timeout.
# Si True, on skip toutes les validations SMTP suivantes.
_SMTP_BLOCKED = False

# Résolveurs DNS publics (évite le DNS FAI souvent lent/bloqué)
_DNS_NAMESERVERS = ["1.1.1.1", "8.8.8.8"]

# Adresses "from" pour le handshake SMTP (domaine neutre)
_SMTP_FROM_DOMAIN = "verifier.example.com"

# Domaines d'email grand public : on ne peut pas deviner les patterns
# (gmail, yahoo, etc. n'ont rien à voir avec l'entreprise)
PUBLIC_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.fr",
    "hotmail.com", "hotmail.fr", "outlook.com", "outlook.fr",
    "live.com", "live.fr", "msn.com", "orange.fr", "wanadoo.fr",
    "free.fr", "laposte.net", "sfr.fr", "bbox.fr", "numericable.fr",
    "icloud.com", "me.com", "mac.com", "aol.com", "aol.fr",
    "protonmail.com", "proton.me", "gmx.fr", "gmx.com",
}


def _normalize_token(s):
    """Retire accents, ponctuation, passe en minuscule."""
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Ne garder que les lettres
    return re.sub(r"[^a-zA-Z]", "", ascii_str).lower()


def _extract_domain(site_web):
    """Extrait le domaine d'un site web (sans www., sans sous-chemins)."""
    if not site_web:
        return ""
    url = site_web if "://" in site_web else "http://" + site_web
    try:
        parsed = urlparse(url)
        host = parsed.netloc or parsed.path
        host = host.lower().strip()
        if host.startswith("www."):
            host = host[4:]
        # Retirer le port éventuel
        host = host.split(":")[0].split("/")[0]
        return host
    except Exception:
        return ""


def generate_patterns(prenom, nom, domain):
    """Génère les patterns d'email les plus probables, par ordre de priorité.

    Retourne une liste d'emails sans duplicats, triée du plus au moins probable.
    """
    p = _normalize_token(prenom)
    n = _normalize_token(nom)
    if not domain or (not p and not n):
        return []

    patterns = []
    # Ordre de priorité pour le B2B FR
    if p and n:
        patterns.append("{}.{}@{}".format(p, n, domain))           # prenom.nom
        patterns.append("{}{}@{}".format(p[0], n, domain))         # pnom
        patterns.append("{}.{}@{}".format(p[0], n, domain))        # p.nom
        patterns.append("{}{}@{}".format(p, n, domain))            # prenomnom
        patterns.append("{}-{}@{}".format(p, n, domain))           # prenom-nom
        patterns.append("{}.{}@{}".format(n, p, domain))           # nom.prenom
        patterns.append("{}_{}@{}".format(p, n, domain))           # prenom_nom
    if p:
        patterns.append("{}@{}".format(p, domain))                 # prenom
    if n:
        patterns.append("{}@{}".format(n, domain))                 # nom

    # Déduplication en préservant l'ordre
    seen = set()
    uniq = []
    for e in patterns:
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    return uniq


def _get_mx_records(domain):
    """Retourne la liste des serveurs MX triés par priorité (croissante).

    Filtre les "null MX" (RFC 7505 : "." ou chaînes trop courtes) qui indiquent
    que le domaine n'accepte pas d'email.
    """
    try:
        resolver = dns.resolver.Resolver()
        resolver.nameservers = _DNS_NAMESERVERS
        resolver.timeout = DNS_TIMEOUT
        resolver.lifetime = DNS_TIMEOUT
        answers = resolver.resolve(domain, "MX")
        mx_records = sorted(
            [(r.preference, str(r.exchange).rstrip(".")) for r in answers],
            key=lambda x: x[0],
        )
        # Filtrer les hosts invalides : "." (null MX), "~", chaînes de 1 caractère
        valid = [host for _, host in mx_records if host and len(host) > 2 and "." in host]
        return valid
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN,
            dns.resolver.NoNameservers, dns.exception.Timeout):
        return []
    except Exception:
        return []


def _smtp_rcpt_check(mx_host, emails_to_test):
    """Ouvre une connexion SMTP à mx_host et teste une liste d'emails.

    Retourne un dict {email: 'valid' | 'invalid' | 'unknown'}.
    Sur erreur de connexion, tous les emails sont marqués 'unknown'
    et _SMTP_BLOCKED passe à True pour éviter les prochains timeouts.
    """
    global _SMTP_BLOCKED
    result = {e: "unknown" for e in emails_to_test}

    if _SMTP_BLOCKED:
        return result

    try:
        server = smtplib.SMTP(timeout=SMTP_TIMEOUT)
        server.connect(mx_host, 25)
        server.helo(_SMTP_FROM_DOMAIN)
        server.mail("verify@" + _SMTP_FROM_DOMAIN)

        for email in emails_to_test:
            try:
                code, _ = server.rcpt(email)
                if code == 250 or code == 251:
                    result[email] = "valid"
                elif code in (550, 551, 553):
                    result[email] = "invalid"
                else:
                    # 450, 451, 452, 4xx : ambiguous (greylisting, temporaire)
                    result[email] = "unknown"
            except smtplib.SMTPServerDisconnected:
                break
            except Exception:
                result[email] = "unknown"

        try:
            server.quit()
        except Exception:
            pass
    except (socket.timeout, ConnectionRefusedError):
        # Port 25 bloqué par le FAI : on désactive SMTP pour les prochains appels
        _SMTP_BLOCKED = True
    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected,
            socket.gaierror, OSError):
        pass
    except Exception:
        pass
    return result


def smtp_available():
    """Retourne True si les checks SMTP semblent fonctionner (port 25 pas bloqué)."""
    return not _SMTP_BLOCKED


def reset_smtp_block():
    """Réinitialise la détection de blocage SMTP (utile pour les tests)."""
    global _SMTP_BLOCKED
    _SMTP_BLOCKED = False


def remote_verify(email, timeout=12):
    """Vérifie un email via le micro-service distant (VPS avec port 25 ouvert).

    Retourne un dict {status: 'valid'|'invalid'|'catchall'|'no_mx'|'unknown', ...}
    ou None si le service n'est pas configuré ou injoignable.
    """
    if not VERIFIER_URL or not VERIFIER_KEY:
        return None
    try:
        resp = requests.get(
            VERIFIER_URL + "/verify",
            params={"email": email, "key": VERIFIER_KEY},
            timeout=timeout,
        )
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException:
        return None
    return None


def remote_check_catchall(domain, timeout=12):
    """Vérifie si un domaine est catchall via le service distant."""
    if not VERIFIER_URL or not VERIFIER_KEY:
        return None
    try:
        resp = requests.get(
            VERIFIER_URL + "/catchall",
            params={"domain": domain, "key": VERIFIER_KEY},
            timeout=timeout,
        )
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException:
        return None
    return None


def remote_available():
    """Retourne True si le service distant est configuré et répond."""
    if not VERIFIER_URL:
        return False
    try:
        resp = requests.get(VERIFIER_URL + "/health", timeout=3)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def _is_catchall(domain, mx_host):
    """Teste si le domaine accepte n'importe quel email (catchall).

    Si oui, la validation SMTP des patterns ne prouve rien.
    """
    fake_local = "zz" + "".join(random.choices("0123456789abcdef", k=16))
    fake_email = "{}@{}".format(fake_local, domain)
    result = _smtp_rcpt_check(mx_host, [fake_email])
    return result.get(fake_email) == "valid"


def _find_via_remote(patterns, max_tests=5):
    """Valide les patterns via le micro-service distant (VPS).

    Retourne un dict {email, confidence, method} ou None si le service a échoué.
    """
    first_result = remote_verify(patterns[0])
    if first_result is None:
        return None

    status = first_result.get("status", "")
    if status == "no_mx":
        return {"email": "", "confidence": "none", "method": "no_mx"}
    if status == "invalid_format":
        return {"email": "", "confidence": "none", "method": "no_domain"}
    if status == "catchall":
        return {"email": patterns[0], "confidence": "medium", "method": "catchall"}
    if status == "valid":
        return {"email": patterns[0], "confidence": "high", "method": "smtp_verified"}

    # status == "invalid" ou "unknown" : tester les patterns suivants
    for pattern in patterns[1:max_tests]:
        r = remote_verify(pattern)
        if r is None:
            return None  # service devenu indisponible, fallback local
        s = r.get("status", "")
        if s == "valid":
            return {"email": pattern, "confidence": "high", "method": "smtp_verified"}
        if s == "catchall":
            return {"email": pattern, "confidence": "medium", "method": "catchall"}

    # Aucun pattern validé : pattern le plus probable en faible confiance
    return {"email": patterns[0], "confidence": "low", "method": "best_guess"}


def find_nominative_email(prenom, nom, site_web):
    """Trouve le meilleur email nominatif pour un dirigeant.

    Retourne un dict :
      {
        'email': str,              # meilleur candidat (vide si aucun)
        'confidence': 'high' | 'medium' | 'low' | 'none',
        'method': str,             # 'smtp_verified' | 'catchall' | 'best_guess' | 'no_domain' | 'public_domain'
      }

    - 'high' : SMTP a confirmé la boîte et le domaine n'est pas catchall
    - 'medium' : SMTP a confirmé mais domaine catchall (tout accepte)
    - 'low' : pas de SMTP possible → pattern le plus probable retourné en 'best guess'
    - 'none' : pas de domaine exploitable
    """
    domain = _extract_domain(site_web)
    if not domain:
        return {"email": "", "confidence": "none", "method": "no_domain"}

    if domain in PUBLIC_EMAIL_DOMAINS:
        return {"email": "", "confidence": "none", "method": "public_domain"}

    patterns = generate_patterns(prenom, nom, domain)
    if not patterns:
        return {"email": "", "confidence": "none", "method": "no_domain"}

    # Mode prioritaire : service distant (VPS avec port 25 ouvert)
    if VERIFIER_URL and VERIFIER_KEY:
        remote_result = _find_via_remote(patterns)
        if remote_result is not None:
            return remote_result
        # Service injoignable : fallback sur le flux local

    # Résolution MX : si pas de MX, le domaine n'a pas d'email configuré
    mx_hosts = _get_mx_records(domain)
    if not mx_hosts:
        return {"email": "", "confidence": "none", "method": "no_mx"}

    # Si le port 25 est déjà connu comme bloqué, ne même pas essayer le SMTP
    if _SMTP_BLOCKED:
        return {"email": patterns[0], "confidence": "medium", "method": "mx_only"}

    mx_host = mx_hosts[0]

    # Détection catchall AVANT de tester les patterns
    catchall = _is_catchall(domain, mx_host)

    # Si la détection catchall a déclenché le blocage SMTP, on bascule
    if _SMTP_BLOCKED:
        return {"email": patterns[0], "confidence": "medium", "method": "mx_only"}

    if catchall:
        return {"email": patterns[0], "confidence": "medium", "method": "catchall"}

    # Tester chaque pattern jusqu'au premier valide (max 5 pour éviter de hammerer)
    to_test = patterns[:5]
    results = _smtp_rcpt_check(mx_host, to_test)

    for pattern in to_test:
        if results.get(pattern) == "valid":
            return {"email": pattern, "confidence": "high", "method": "smtp_verified"}

    # Si le SMTP a été détecté comme bloqué pendant les tests, on tombe en mx_only
    if _SMTP_BLOCKED:
        return {"email": patterns[0], "confidence": "medium", "method": "mx_only"}

    # MX existe mais aucun pattern confirmé → pattern probable en faible confiance
    return {"email": patterns[0], "confidence": "low", "method": "best_guess"}


def validate_email(email):
    """Valide un email scrapé via le service SMTP distant.

    Retourne un dict : {status, confidence}
      - status : 'valid' | 'catchall' | 'invalid' | 'no_mx' | 'unknown' | 'public'
      - confidence : 'high' | 'medium' | 'low' | 'none'
    """
    if not email or "@" not in email:
        return {"status": "invalid", "confidence": "none"}

    email = email.strip().lower()
    domain = email.split("@", 1)[1]

    # Domaines publics : impossibles à valider fiablement (gmail etc.
    # acceptent tout côté MX mais ça ne prouve pas l'existence)
    if domain in PUBLIC_EMAIL_DOMAINS:
        return {"status": "public", "confidence": "medium"}

    # Passage par le VPS
    if VERIFIER_URL and VERIFIER_KEY:
        result = remote_verify(email)
        if result is not None:
            s = result.get("status", "unknown")
            if s == "valid":
                return {"status": "valid", "confidence": "high"}
            if s == "catchall":
                return {"status": "catchall", "confidence": "medium"}
            if s == "invalid":
                return {"status": "invalid", "confidence": "none"}
            if s == "no_mx":
                return {"status": "no_mx", "confidence": "none"}
            return {"status": "unknown", "confidence": "low"}

    # Pas de VPS : on ne peut pas trancher, on garde l'email sans confidence
    return {"status": "unknown", "confidence": "low"}


def validate_scraped_emails(entreprises, progress_callback=None, workers=5):
    """Valide les emails scrapés de chaque entreprise via le VPS SMTP.

    Ajoute deux clés sur chaque entreprise :
      - 'email_status' : valid / catchall / invalid / no_mx / unknown / public
      - 'email_confidence' : high / medium / low / none
    """
    # Grouper par email unique pour ne valider qu'une fois chaque adresse
    unique_emails = set()
    for e in entreprises:
        email = (e.get("emails", "") or "").strip().lower()
        if email:
            unique_emails.add(email)

    total = len(unique_emails)
    if progress_callback:
        progress_callback("Validation SMTP de %d emails uniques..." % total, 0.0)

    results = {}
    completed = [0]

    def _task(email):
        return email, validate_email(email)

    if unique_emails:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_task, email) for email in unique_emails]
            for future in as_completed(futures):
                try:
                    email, result = future.result()
                    results[email] = result
                except Exception:
                    pass
                completed[0] += 1
                if progress_callback:
                    progress_callback(
                        "Validation email %d/%d" % (completed[0], total),
                        completed[0] / max(total, 1),
                    )

    # NE JAMAIS dropper l'email. On enregistre juste le statut SMTP, l'UI
    # l'affichera avec un badge (rouge si invalid, orange si unknown, vert
    # si valid). Drop = perte de données = mauvaise idée.
    for e in entreprises:
        email = (e.get("emails", "") or "").strip().lower()
        if email and email in results:
            e["email_status"] = results[email]["status"]
            e["email_confidence"] = results[email]["confidence"]
        else:
            e["email_status"] = ""
            e["email_confidence"] = ""

    if progress_callback:
        progress_callback("Validation emails terminee !", 1.0)

    return entreprises


def enrich_nominative_emails(entreprises, progress_callback=None):
    """Enrichit les entreprises avec un email nominatif dirigeant.

    Prérequis : les entreprises doivent avoir été enrichies au préalable
    par entreprise_enricher (dirigeant_prenom, dirigeant_nom) et avoir un site_web.

    Ajoute deux clés :
      - 'email_dirigeant' : l'email généré ou vérifié (peut être vide)
      - 'email_dirigeant_confidence' : 'high' | 'medium' | 'low' | 'none'
    """
    total = len(entreprises)
    if progress_callback:
        progress_callback("Recherche emails dirigeants sur %d entreprises..." % total, 0.0)

    # Grouper par domaine pour mutualiser les connexions SMTP
    last_domain = None
    last_mx = None

    for idx, entreprise in enumerate(entreprises):
        prenom = entreprise.get("dirigeant_prenom", "")
        nom = entreprise.get("dirigeant_nom", "")
        site_web = entreprise.get("site_web", "")

        if progress_callback:
            progress_callback(
                "Email dirigeant %d/%d : %s" % (idx + 1, total, entreprise.get("nom", "")),
                (idx + 1) / max(total, 1),
            )

        if not prenom and not nom:
            entreprise["email_dirigeant"] = ""
            entreprise["email_dirigeant_confidence"] = "none"
            continue

        result = find_nominative_email(prenom, nom, site_web)
        entreprise["email_dirigeant"] = result["email"]
        entreprise["email_dirigeant_confidence"] = result["confidence"]

        # Petit délai pour ne pas hammerer le même serveur SMTP
        domain = _extract_domain(site_web)
        if domain and domain == last_domain:
            time.sleep(0.5)
        last_domain = domain

    if progress_callback:
        progress_callback("Emails dirigeants termine !", 1.0)

    return entreprises
