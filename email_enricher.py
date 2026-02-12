"""Extraction d'emails depuis les sites web des entreprises."""

import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

# Timeout pour les requêtes HTTP
REQUEST_TIMEOUT = 10

# Regex pour extraire les emails
EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')

# Emails à ignorer (faux positifs courants)
IGNORED_PATTERNS = {
    "example.com", "example.org", "example.net",
    "sentry.io", "wixpress.com", "googleapis.com",
    "w3.org", "schema.org", "wordpress.org",
    "gravatar.com", "wp.com",
}

IGNORED_PREFIXES = {
    "image", "img", "photo", "icon", "logo",
    "noreply", "no-reply", "mailer-daemon",
    "postmaster", "webmaster", "root",
}


def is_valid_email(email: str) -> bool:
    """Vérifie qu'un email n'est pas un faux positif."""
    email = email.lower().strip()

    # Vérifier le domaine
    domain = email.split("@")[1] if "@" in email else ""
    if any(ignored in domain for ignored in IGNORED_PATTERNS):
        return False

    # Vérifier le préfixe
    prefix = email.split("@")[0]
    if any(prefix.startswith(p) for p in IGNORED_PREFIXES):
        return False

    # Ignorer les emails qui ressemblent à des fichiers
    if domain.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".css", ".js")):
        return False

    return True


def extract_emails_from_url(url: str) -> list[str]:
    """Extrait les emails d'une URL donnée."""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers=headers, allow_redirects=True)
        resp.raise_for_status()

        # Extraire les emails du HTML brut
        emails = set(EMAIL_REGEX.findall(resp.text))

        # Aussi chercher les mailto: links
        soup = BeautifulSoup(resp.text, "html.parser")
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if href.startswith("mailto:"):
                email = href.replace("mailto:", "").split("?")[0].strip()
                if email:
                    emails.add(email)

        return [e for e in emails if is_valid_email(e)]

    except Exception:
        return []


def enrich_emails(entreprises: list[dict], progress_callback=None) -> list[dict]:
    """
    Enrichit une liste d'entreprises avec les emails trouvés sur leurs sites web.

    Args:
        entreprises: Liste de dicts avec clé 'site_web'
        progress_callback: Fonction appelée avec (message, progression 0-1)

    Returns:
        La même liste avec une clé 'emails' ajoutée
    """
    sites_to_check = [e for e in entreprises if e.get("site_web")]
    total = len(sites_to_check)

    if progress_callback:
        progress_callback(f"Recherche d'emails sur {total} sites web...", 0.0)

    for idx, entreprise in enumerate(entreprises):
        site = entreprise.get("site_web", "")
        if not site:
            entreprise["emails"] = ""
            continue

        if progress_callback:
            progress_callback(
                f"Scan email {idx+1}/{total} : {entreprise.get('nom', '')}",
                idx / max(total, 1)
            )

        all_emails = set()

        # Page d'accueil
        all_emails.update(extract_emails_from_url(site))

        # Page contact
        for contact_path in ["/contact", "/contact/", "/nous-contacter", "/contactez-nous"]:
            contact_url = urljoin(site, contact_path)
            all_emails.update(extract_emails_from_url(contact_url))
            if all_emails:
                break  # On a trouvé des emails, pas besoin de tester les autres

        entreprise["emails"] = ", ".join(sorted(all_emails))

    if progress_callback:
        progress_callback("Enrichissement email terminé !", 1.0)

    return entreprises
