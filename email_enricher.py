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

# Préfixes d'emails prioritaires (dirigeant / contact principal), par ordre de priorité
PRIORITY_PREFIXES = [
    "direction", "directeur", "dirigeant", "gerant", "patron",
    "contact", "info", "accueil", "bonjour", "hello",
    "commercial", "devis",
]

# Pages contact (tier 1 — prioritaires)
CONTACT_PATHS = [
    "/contact", "/contact/", "/nous-contacter", "/contactez-nous",
    "/contact-us", "/contactez-nous/",
]

# Pages secondaires (tier 2)
SECONDARY_PATHS = [
    "/a-propos", "/about", "/about-us", "/about/",
    "/equipe", "/team", "/qui-sommes-nous",
    "/mentions-legales", "/legal", "/mentions-legales/",
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
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
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers=_HEADERS, allow_redirects=True)
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


def pick_best_email(emails: set) -> str:
    """Sélectionne le meilleur email parmi un ensemble (dirigeant/contact principal)."""
    if not emails:
        return ""
    if len(emails) == 1:
        return next(iter(emails))

    # Trier par priorité : d'abord les préfixes prioritaires
    for prefix in PRIORITY_PREFIXES:
        for email in emails:
            if email.lower().split("@")[0].startswith(prefix):
                return email

    # Sinon, préférer les emails courts (souvent plus génériques : prenom@domain)
    # et exclure ceux qui ressemblent à des emails d'équipe (prenom.nom long)
    sorted_emails = sorted(emails, key=lambda e: len(e.split("@")[0]))
    return sorted_emails[0]


def extract_footer_contact_links(url: str) -> list[str]:
    """Extrait les liens contact/about depuis le footer d'une page."""
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers=_HEADERS, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        footer_links = []
        footer_els = soup.find_all("footer")
        footer_els += soup.find_all(attrs={"class": re.compile(r"footer", re.I)})
        footer_els += soup.find_all(attrs={"id": re.compile(r"footer", re.I)})

        contact_keywords = ["contact", "about", "propos", "equipe", "team", "legal", "mention"]

        seen = set()
        for footer in footer_els:
            for a_tag in footer.find_all("a", href=True):
                href = a_tag["href"].lower()
                if any(kw in href for kw in contact_keywords):
                    full_url = urljoin(url, a_tag["href"])
                    if full_url not in seen:
                        seen.add(full_url)
                        footer_links.append(full_url)

        return footer_links

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

        # 1. Page d'accueil (toujours scannée)
        all_emails.update(extract_emails_from_url(site))

        # 2. Pages contact (tier 1) — arrêt dès qu'une sous-page donne des emails
        found_on_subpage = False
        for contact_path in CONTACT_PATHS:
            contact_url = urljoin(site, contact_path)
            page_emails = extract_emails_from_url(contact_url)
            if page_emails:
                all_emails.update(page_emails)
                found_on_subpage = True
                break

        # 3. Si rien sur les pages contact, scanner les liens du footer
        if not found_on_subpage:
            footer_links = extract_footer_contact_links(site)
            for link_url in footer_links[:3]:
                page_emails = extract_emails_from_url(link_url)
                if page_emails:
                    all_emails.update(page_emails)
                    found_on_subpage = True
                    break

        # 4. Si toujours rien, essayer les pages secondaires (tier 2)
        if not found_on_subpage:
            for path in SECONDARY_PATHS:
                page_emails = extract_emails_from_url(urljoin(site, path))
                if page_emails:
                    all_emails.update(page_emails)
                    break

        entreprise["emails"] = pick_best_email(all_emails)

    if progress_callback:
        progress_callback("Enrichissement email terminé !", 1.0)

    return entreprises
