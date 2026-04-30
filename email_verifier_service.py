"""Micro-service de vérification d'emails via SMTP RCPT TO.

Déployé sur un VPS avec port 25 ouvert (Contabo).
Exposé en HTTP sur le port 8000, sécurisé par une clé API.

Endpoints :
  GET /health                     → {"status": "ok"}
  GET /verify?email=X&key=Y       → validation complète de l'email
  GET /catchall?domain=X&key=Y    → détection catchall sur un domaine
"""

import json
import os
import random
import re
import smtplib
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import dns.resolver
import dns.exception


API_KEY = os.environ.get("VERIFIER_API_KEY", "")
BIND_HOST = "0.0.0.0"
BIND_PORT = 8000

SMTP_TIMEOUT = 8
DNS_TIMEOUT = 4
_SMTP_FROM = "verify@mail-check.io"

EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


def get_mx(domain):
    try:
        resolver = dns.resolver.Resolver()
        resolver.timeout = DNS_TIMEOUT
        resolver.lifetime = DNS_TIMEOUT
        answers = resolver.resolve(domain, "MX")
        mx = sorted(
            [(r.preference, str(r.exchange).rstrip(".")) for r in answers],
            key=lambda x: x[0],
        )
        return [h for _, h in mx if h and len(h) > 2 and "." in h]
    except Exception:
        return []


def smtp_rcpt(mx_host, emails):
    """Teste une liste d'emails sur un serveur MX. Retourne {email: code}."""
    result = {e: "unknown" for e in emails}
    try:
        server = smtplib.SMTP(timeout=SMTP_TIMEOUT)
        server.connect(mx_host, 25)
        server.helo(_SMTP_FROM.split("@")[1])
        server.mail(_SMTP_FROM)
        for email in emails:
            try:
                code, _ = server.rcpt(email)
                if code in (250, 251):
                    result[email] = "valid"
                elif code in (550, 551, 553):
                    result[email] = "invalid"
                else:
                    result[email] = "unknown"
            except smtplib.SMTPServerDisconnected:
                break
            except Exception:
                result[email] = "unknown"
        try:
            server.quit()
        except Exception:
            pass
    except Exception:
        pass
    return result


def is_catchall(domain, mx_host):
    fake = "zz" + "".join(random.choices("0123456789abcdef", k=16)) + "@" + domain
    r = smtp_rcpt(mx_host, [fake])
    return r.get(fake) == "valid"


def verify_email(email):
    """Retourne un dict avec le statut complet de vérification."""
    if not EMAIL_RE.match(email or ""):
        return {"email": email, "status": "invalid_format"}

    domain = email.split("@")[1].lower()
    mx = get_mx(domain)
    if not mx:
        return {"email": email, "status": "no_mx", "domain": domain}

    mx_host = mx[0]
    catchall = is_catchall(domain, mx_host)

    if catchall:
        return {
            "email": email, "status": "catchall",
            "mx_host": mx_host, "domain": domain,
        }

    result = smtp_rcpt(mx_host, [email])
    return {
        "email": email,
        "status": result.get(email, "unknown"),
        "mx_host": mx_host,
        "domain": domain,
        "catchall": False,
    }


def check_catchall(domain):
    mx = get_mx(domain)
    if not mx:
        return {"domain": domain, "status": "no_mx"}
    return {
        "domain": domain,
        "mx_host": mx[0],
        "catchall": is_catchall(domain, mx[0]),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Log compact
        print("[%s] %s" % (self.address_string(), fmt % args))

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/health":
            return self._json(200, {"status": "ok"})

        # Toutes les autres routes nécessitent la clé API
        key = (qs.get("key") or [""])[0]
        if not API_KEY or key != API_KEY:
            return self._json(401, {"error": "invalid_key"})

        if parsed.path == "/verify":
            email = (qs.get("email") or [""])[0].strip().lower()
            if not email:
                return self._json(400, {"error": "missing_email"})
            return self._json(200, verify_email(email))

        if parsed.path == "/catchall":
            domain = (qs.get("domain") or [""])[0].strip().lower()
            if not domain:
                return self._json(400, {"error": "missing_domain"})
            return self._json(200, check_catchall(domain))

        return self._json(404, {"error": "not_found"})


def main():
    if not API_KEY:
        print("ERROR: VERIFIER_API_KEY env var must be set")
        raise SystemExit(1)

    # Évite les "Address already in use" au restart
    ThreadingHTTPServer.allow_reuse_address = True

    server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler)
    print("Listening on %s:%d" % (BIND_HOST, BIND_PORT))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
