"""Scraping des emails publiés sur le site officiel d'une entreprise.

Sortie : pour chaque entreprise, `entreprise["emails"]` est une **liste de
dicts** (jamais une string), chaque dict ayant la forme :

    {email, source: "published", source_url, smtp_status, is_public_domain,
     destinataire_rank}

Le SMTP n'est PAS appelé ici — c'est le pipeline qui s'en charge ensuite.
Les emails sur un domaine tiers (≠ domaine du site officiel ET ≠ public)
sont filtrés : ce sont typiquement des emails d'agence web / hébergeur
parasites présents dans le footer.
"""

import gc
import re

import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

from email_processor import PUBLIC_EMAIL_DOMAINS, is_public_email
from validators import extract_domain


REQUEST_TIMEOUT = 8
MAX_RESPONSE_SIZE = 1_000_000
CONTENT_MAX_LENGTH = 3000
GC_BATCH_SIZE = 200

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
# Échappements JSON Unicode courants qui leak dans le HTML scrappé
_JSON_ESCAPE_RE = re.compile(r"\\u00([0-9a-fA-F]{2})")

# Plateformes / templates / placeholders → emails à ignorer
PLACEHOLDER_DOMAINS = {
    "domain.com", "exemple.com", "exemple.fr", "example.fr", "example.com",
    "example.org", "example.net", "mydomain.com", "votredomaine.com",
    "votredomaine.fr", "test.fr", "domaine.com", "domaine.fr",
    "local.fr", "etre-visible.local.fr",
    "webador.fr", "mapszi.com", "centralapp.com",
    "jesorsenville.com", "lateliercom.fr",
    "sentry.io", "wixpress.com", "googleapis.com",
    "w3.org", "schema.org", "wordpress.org",
    "gravatar.com", "wp.com",
}

IGNORED_PREFIXES = {
    "image", "img", "photo", "icon", "logo",
    "noreply", "no-reply", "mailer-daemon",
    "postmaster", "webmaster", "root",
}

PLACEHOLDER_EMAILS = {
    "nom@domain.com", "votre@email.com", "utilisateur@domaine.com",
    "email@domain.com", "email@email.com", "email@example.com",
    "exemple@exemple.com", "example@example.com", "test@test.com",
    "name@example.com", "you@example.com", "your@email.com",
    "contact@example.com", "info@example.com", "hello@example.com",
    "john@doe.com", "jane@doe.com", "john.doe@email.com",
    "adresse@mail.com", "votre.nom@exemple.com",
}

CONTACT_PATHS = [
    "/contact", "/contact/", "/nous-contacter", "/contactez-nous",
    "/contact-us", "/contactez-nous/",
]

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


def _create_session():
    session = requests.Session()
    session.headers.update(_HEADERS)
    adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def is_valid_email(email):
    """Filtre les faux positifs évidents (placeholders, préfixes système).
    On NE filtre PAS ici les domaines tiers — c'est le rôle de l'appelant."""
    if not email or "@" not in email:
        return False
    email = email.lower().strip()
    if email in PLACEHOLDER_EMAILS:
        return False
    local, _, domain = email.partition("@")
    if domain in PLACEHOLDER_DOMAINS:
        return False
    if any(local.startswith(p) for p in IGNORED_PREFIXES):
        return False
    if domain.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".css", ".js")):
        return False
    return True


def _decode_json_escapes(text):
    """Convertit `\\u003e` etc. en caractères réels avant extraction d'email."""
    return _JSON_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)


def _fetch(url, session, with_text=False):
    """GET avec limite de taille. Retourne (emails_set, clean_text or '').
    Les mailto: sont aussi récupérés."""
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT, headers=_HEADERS,
                           allow_redirects=True, stream=True)
        resp.raise_for_status()
        content = resp.raw.read(MAX_RESPONSE_SIZE, decode_content=True)
        resp.close()
        text = _decode_json_escapes(content.decode("utf-8", errors="ignore"))

        emails = set(EMAIL_REGEX.findall(text))
        soup = BeautifulSoup(text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("mailto:"):
                e = href.replace("mailto:", "").split("?")[0].strip()
                if e:
                    emails.add(e)

        clean_text = ""
        if with_text:
            for tag in soup(["script", "style", "noscript", "nav",
                             "footer", "header", "svg"]):
                tag.decompose()
            clean_text = soup.get_text(separator=" ", strip=True)
            clean_text = re.sub(r"\s+", " ", clean_text)[:CONTENT_MAX_LENGTH].strip()

        del soup, text, content
        return {e for e in emails if is_valid_email(e)}, clean_text
    except Exception:
        return set(), ""


def _extract_footer_contact_links(url, session):
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT, headers=_HEADERS,
                           allow_redirects=True, stream=True)
        resp.raise_for_status()
        content = resp.raw.read(MAX_RESPONSE_SIZE, decode_content=True)
        resp.close()
        text = content.decode("utf-8", errors="ignore")
        soup = BeautifulSoup(text, "html.parser")
        del content, text

        footer_els = soup.find_all("footer")
        footer_els += soup.find_all(attrs={"class": re.compile(r"footer", re.I)})
        footer_els += soup.find_all(attrs={"id": re.compile(r"footer", re.I)})

        contact_keywords = ["contact", "about", "propos", "equipe", "team",
                             "legal", "mention"]
        seen = set()
        links = []
        for footer in footer_els:
            for a in footer.find_all("a", href=True):
                href = a["href"].lower()
                if any(kw in href for kw in contact_keywords):
                    full = urljoin(url, a["href"])
                    if full not in seen:
                        seen.add(full)
                        links.append(full)
        return links
    except Exception:
        return []


def _enrich_one(entreprise, session):
    """Scrape le site de l'entreprise et remplit entreprise['emails'] (list[dict])
    et entreprise['contenu_site']."""
    site = (entreprise.get("site_web") or "").strip()
    if "emails" not in entreprise:
        entreprise["emails"] = []
    if "contenu_site" not in entreprise:
        entreprise["contenu_site"] = ""
    if not site:
        return

    site_domain = extract_domain(site) or ""
    if not site_domain:
        return

    # 1. Home (avec extraction texte)
    home_emails, home_text = _fetch(site, session, with_text=True)
    if home_text:
        entreprise["contenu_site"] = home_text

    all_emails = set(home_emails)
    sources = {e: site for e in home_emails}

    # 2. Pages contact tier 1
    found_subpage = False
    for path in CONTACT_PATHS:
        page_url = urljoin(site, path)
        emails, _ = _fetch(page_url, session)
        if emails:
            for e in emails:
                if e not in sources:
                    sources[e] = page_url
            all_emails.update(emails)
            found_subpage = True
            break

    # 3. Footer links
    if not found_subpage:
        for link in _extract_footer_contact_links(site, session)[:3]:
            emails, _ = _fetch(link, session)
            if emails:
                for e in emails:
                    if e not in sources:
                        sources[e] = link
                all_emails.update(emails)
                found_subpage = True
                break

    # 4. Pages secondaires (à-propos, équipe, mentions légales)
    if not found_subpage:
        for path in SECONDARY_PATHS:
            page_url = urljoin(site, path)
            emails, _ = _fetch(page_url, session)
            if emails:
                for e in emails:
                    if e not in sources:
                        sources[e] = page_url
                all_emails.update(emails)
                break

    # Filtre domaine : on garde
    #   - les emails @<domaine_site>  (et sous-domaines)
    #   - les emails sur domaine public (gmail/...) — artisan solo
    # On rejette tout le reste (agence web, hébergeur, partenaires…)
    kept = []
    seen_emails = set()
    for raw in all_emails:
        e = raw.strip().lower().rstrip(".")
        if e in seen_emails or "@" not in e:
            continue
        seen_emails.add(e)
        dom = e.split("@", 1)[1]
        is_pub = dom in PUBLIC_EMAIL_DOMAINS
        same_domain = (dom == site_domain) or dom.endswith("." + site_domain)
        if not is_pub and not same_domain:
            continue  # 3rd-party noise (agence web, hosting…)
        kept.append({
            "email": e,
            "source": "published",
            "source_url": sources.get(raw, site),
            "smtp_status": "",
            "is_public_domain": is_pub,
            "destinataire_rank": 4,  # rang provisoire, calculé proprement plus tard
        })

    entreprise["emails"] = kept


def enrich_emails(entreprises, progress_callback=None):
    """Scrape les sites validés et remplit `entreprise['emails']` (list[dict]).
    Modifie en place et retourne la liste. Ne supprime AUCUNE entreprise.
    """
    sites_to_check = [e for e in entreprises if e.get("site_web")]
    total = len(sites_to_check)
    if progress_callback:
        progress_callback("Recherche d'emails sur %d sites web..." % total, 0.0)

    session = _create_session()
    try:
        site_idx = 0
        for idx, entreprise in enumerate(entreprises):
            if "emails" not in entreprise:
                entreprise["emails"] = []
            if not entreprise.get("site_web"):
                continue
            site_idx += 1
            if progress_callback:
                progress_callback(
                    "Scan email %d/%d : %s" % (site_idx, total, entreprise.get("nom", "")),
                    site_idx / max(total, 1),
                )
            try:
                _enrich_one(entreprise, session)
            except Exception:
                # Ne jamais casser : entreprise conservée, juste sans email
                if "emails" not in entreprise:
                    entreprise["emails"] = []
            if site_idx % GC_BATCH_SIZE == 0:
                gc.collect()
    finally:
        session.close()

    if progress_callback:
        progress_callback("Enrichissement email terminé !", 1.0)
    return entreprises
