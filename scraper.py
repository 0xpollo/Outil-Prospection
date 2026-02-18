"""Scraping Google Maps pour récupérer les coordonnées d'entreprises.

Méthode principale : requête HTTP directe au endpoint tbm=map de Google.
Fallback : Selenium headless Chrome (plus lent mais plus robuste).
"""

import json
import time
import re
import requests as req_lib
from urllib.parse import quote_plus
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager


def normalize_phone(raw):
    """Normalise un numéro français au format '06 12 34 56 78'."""
    if not raw:
        return ""
    digits = re.sub(r"[^\d+]", "", raw)
    if digits.startswith("+33"):
        digits = "0" + digits[3:]
    elif digits.startswith("33") and len(digits) == 11:
        digits = "0" + digits[2:]
    if len(digits) == 10 and digits.startswith("0"):
        return "{} {} {} {} {}".format(
            digits[0:2], digits[2:4], digits[4:6], digits[6:8], digits[8:10]
        )
    return raw


# ─────────────────────────────────────────────────────────────────────────────
# Méthode 1 : HTTP direct (rapide, ~2 secondes)
# ─────────────────────────────────────────────────────────────────────────────

def _build_pb_param(lat, lng, zoom_meters=40000, max_results=20):
    """Construit le paramètre pb= pour l'endpoint tbm=map de Google."""
    w, h = 1024, 768
    return (
        "!4m8!1m3!1d{zm}!2d{lng}!3d{lat}"
        "!3m2!1i{w}!2i{h}!4f13.1"
        "!7i{mr}!10b1"
        "!12m25!1m5!18b1!30b1!31m1!1b1!34e1"
        "!2m4!5m1!6e2!20e3!39b1"
        "!10b1!12b1!13b1!16b1!17m1!3e1"
        "!20m3!5e2!6b1!14b1"
        "!46m1!1b0!96b1!99b1"
        "!19m4!2m3!1i360!2i120!4i8"
        "!20m65!2m2!1i203!2i100"
        "!3m2!2i4!5b1!4m2!2i4!5b1!6m2!2i4!5b1!7m2!2i4!5b1"
        "!8m2!2i4!5b1!9m2!2i4!5b1!10m2!2i4!5b1!11m2!2i4!5b1"
        "!12m2!2i4!5b1!13m2!2i4!5b1!14m2!2i4!5b1!15m2!2i4!5b1"
        "!16m2!2i4!5b1!17m2!2i4!5b1!18m2!2i4!5b1!19m2!2i4!5b1"
        "!20m2!2i4!5b1"
        "!26m4!2m3!1i80!2i92!4i8"
        "!30m1!2b1!36b1!43b1!52b1!55b1"
        "!34m18!2b1!3b1!4b1!6b1!8m5!1b1!3b1!4b1!5b1!6b1"
        "!9b1!12b1!14b1!20b0!23b1!25b1!26b1!30b0"
        "!37m1!1e81!42b1!47m0"
        "!49m7!3b1!6m2!1b1!2b1!7m2!1e3!2b1"
    ).format(zm=zoom_meters, lng=lng, lat=lat, w=w, h=h, mr=max_results)


def _parse_business(biz):
    """Extrait les infos d'un business depuis son tableau de données (~260 éléments).

    Indices connus :
      [2]  : adresse [rue, ville]
      [4]  : [null*7, note]
      [7]  : [url_site, ...]
      [11] : nom
      [18] : adresse complète (string)
      [178]: téléphone
    """
    if not isinstance(biz, list) or len(biz) < 12:
        return None

    nom = biz[11] if len(biz) > 11 and isinstance(biz[11], str) else None
    if not nom:
        return None

    info = {
        "nom": nom,
        "adresse": "",
        "telephone": "",
        "telephone_raw": "",
        "site_web": "",
        "note": "",
        "nb_avis": 0,
    }

    # Note (index 4, sous-index 7)
    if len(biz) > 4 and isinstance(biz[4], list):
        arr = biz[4]
        if len(arr) > 7 and isinstance(arr[7], (int, float)):
            info["note"] = str(arr[7])

    # Adresse : préférer [2] (composants propres) à [18] (inclut parfois le nom)
    if len(biz) > 2 and isinstance(biz[2], list):
        parts = [p for p in biz[2] if isinstance(p, str)]
        if parts:
            info["adresse"] = ", ".join(parts)
    if not info["adresse"] and len(biz) > 18 and isinstance(biz[18], str):
        # Retirer le nom du business du début de l'adresse si présent
        addr = biz[18]
        if addr.startswith(nom):
            addr = addr[len(nom):].lstrip(", ")
        info["adresse"] = addr

    # Site web (index 7)
    if len(biz) > 7 and isinstance(biz[7], list):
        if len(biz[7]) > 0 and isinstance(biz[7][0], str):
            info["site_web"] = biz[7][0]

    # Téléphone (index 178)
    raw_phone = ""
    if len(biz) > 178 and isinstance(biz[178], list):
        pd = biz[178]
        if len(pd) > 0:
            if isinstance(pd[0], list) and len(pd[0]) > 0 and isinstance(pd[0][0], str):
                raw_phone = pd[0][0]
            elif isinstance(pd[0], str):
                raw_phone = pd[0]
    info["telephone_raw"] = raw_phone
    info["telephone"] = normalize_phone(raw_phone)

    return info


def _find_businesses(data):
    """Trouve les tableaux business dans la réponse JSON de Google Maps.

    La structure varie : data[0][1][N][14] (HTTP direct) ou data[64][N][1] (Playwright).
    On utilise une recherche récursive bornée pour couvrir les deux cas.
    """
    results = []
    seen = set()

    def _check_biz(arr):
        """Tente d'extraire un business depuis un tableau."""
        info = _parse_business(arr)
        if info and info["nom"] not in seen:
            seen.add(info["nom"])
            results.append(info)

    def _search(obj, depth):
        if depth > 6 or not isinstance(obj, list):
            return
        # Est-ce un tableau business ? (150+ éléments, string à index 11)
        if len(obj) > 150 and len(obj) > 11 and isinstance(obj[11], str):
            _check_biz(obj)
            return
        for item in obj:
            _search(item, depth + 1)

    _search(data, 0)
    return results


def _create_http_session():
    """Crée une session HTTP avec les headers et cookies nécessaires."""
    session = req_lib.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/144.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "fr-FR,fr;q=0.9",
        "Referer": "https://www.google.com/maps/",
    })
    session.cookies.set(
        "SOCS",
        "CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjMwODI5LjA3X3AxGgJmciADGgYIgJnPpwY",
        domain=".google.com",
    )
    session.cookies.set("CONSENT", "YES+FR.fr+V14+BX", domain=".google.com")
    return session


def _http_fetch_businesses(session, query, lat, lng, max_results=200,
                           zoom_meters=40000):
    """Une seule requête HTTP, retourne la liste de business ou []."""
    pb = _build_pb_param(lat, lng, zoom_meters=zoom_meters, max_results=max_results)
    url = (
        "https://www.google.com/search?"
        "tbm=map&authuser=0&hl=fr&gl=fr"
        "&q={q}&pb={pb}"
    ).format(q=req_lib.utils.quote(query), pb=pb)

    try:
        resp = session.get(url, timeout=20)
    except req_lib.RequestException:
        return []

    if resp.status_code != 200 or len(resp.text) < 10000:
        return []

    text = resp.text
    if text.startswith(")]}'"):
        text = text[text.index('\n') + 1:]

    decoder = json.JSONDecoder()
    try:
        data, _ = decoder.raw_decode(text)
    except (json.JSONDecodeError, ValueError):
        return []

    if isinstance(data, dict) and 'd' in data:
        d_text = data['d']
        if isinstance(d_text, str):
            if d_text.startswith(")]}'"):
                d_text = d_text[d_text.index('\n') + 1:]
            try:
                data, _ = decoder.raw_decode(d_text)
            except (json.JSONDecodeError, ValueError):
                return []

    return _find_businesses(data)


def _multi_search(session, query, points, progress_callback,
                  progress_start=0.10, progress_end=0.80, label_prefix=""):
    """Effectue plusieurs recherches HTTP et accumule les résultats uniques.

    Parcourt TOUS les points de recherche avant de retourner.
    S'arrête uniquement si 8 recherches consécutives n'apportent rien de nouveau.

    Args:
        points: liste de (lat, lng, zoom_meters, label)
    Returns:
        liste de dicts business (dédupliqués par nom)
    """
    all_results = []
    seen_names = set()
    consecutive_empty = 0

    for i, (plat, plng, zoom, point_label) in enumerate(points):
        if progress_callback:
            pct = progress_start + (progress_end - progress_start) * (i / len(points))
            progress_callback(
                "{}{} — {} entreprises trouvées".format(
                    label_prefix,
                    point_label,
                    len(all_results),
                ),
                pct,
            )

        batch = _http_fetch_businesses(
            session, query, plat, plng,
            max_results=200, zoom_meters=zoom,
        )

        new_count = 0
        for biz in batch:
            if biz["nom"] not in seen_names:
                seen_names.add(biz["nom"])
                all_results.append(biz)
                new_count += 1

        if new_count == 0:
            consecutive_empty += 1
        else:
            consecutive_empty = 0

        # Arrêter si 8 recherches consécutives sans nouveau résultat
        if consecutive_empty >= 8:
            break

        # Pause entre les requêtes
        if i > 0:
            time.sleep(0.3)

    return all_results


def _scrape_via_http(query, lat, lng, max_results=20, mode="simple",
                     progress_callback=None):
    """Scrape Google Maps via requête HTTP directe au endpoint tbm=map.

    Modes:
        "simple"       — 1 requête, ~2s
        "approfondie"  — grille autour du point, ~15s
        "france"       — grille couvrant toute la France, ~2-5 min

    Retourne une liste de dicts ou None si la méthode échoue.
    """
    if progress_callback:
        progress_callback("Recherche en cours...", 0.05)

    session = _create_http_session()

    if mode == "simple":
        results = _http_fetch_businesses(
            session, query, lat, lng,
            max_results=min(max_results, 200),
        )
        if progress_callback and results:
            progress_callback(
                "{} entreprises trouvées".format(len(results)), 0.60
            )
        return results if results else None

    if mode == "approfondie":
        # Grille autour du point central (~10 km entre chaque point)
        offsets = [
            (0, 0, 40000),
            (0.04, 0, 20000), (-0.04, 0, 20000),
            (0, 0.06, 20000), (0, -0.06, 20000),
            (0.04, 0.06, 20000), (0.04, -0.06, 20000),
            (-0.04, 0.06, 20000), (-0.04, -0.06, 20000),
            (0.08, 0, 20000), (-0.08, 0, 20000),
            (0, 0.12, 20000), (0, -0.12, 20000),
            (0.08, 0.06, 20000), (0.08, -0.06, 20000),
            (-0.08, 0.06, 20000), (-0.08, -0.06, 20000),
            (0.04, 0.12, 20000), (0.04, -0.12, 20000),
            (-0.04, 0.12, 20000), (-0.04, -0.12, 20000),
        ]
        points = [
            (lat + dlat, lng + dlng, zm,
             "Recherche {}".format(idx + 1))
            for idx, (dlat, dlng, zm) in enumerate(offsets)
        ]
        results = _multi_search(
            session, query, points, progress_callback,
        )
        if progress_callback:
            progress_callback(
                "{} entreprises trouvées".format(len(results)), 0.80
            )
        return results if results else None

    if mode == "france":
        return _scrape_france(session, query, progress_callback)

    return None


def _scrape_france(session, query, progress_callback):
    """Recherche sur toute la France via une grille de communes.

    Utilise les 300 plus grandes communes comme points de recherche.
    Parcourt TOUTES les villes avant de retourner (pas d'arrêt anticipé
    sur max_results — la troncature se fait dans scrape_google_maps).
    """
    communes_path = __import__("pathlib").Path(__file__).parent / "communes_france.json"
    if not communes_path.exists():
        return None

    with open(str(communes_path), encoding="utf-8") as f:
        communes = json.load(f)

    # Top 300 communes (triées par population dans le fichier)
    points = []
    for label, clat, clng in communes[:300]:
        ville = label.split("(")[0].strip() if "(" in label else label
        points.append((clat, clng, 30000, ville))

    if progress_callback:
        progress_callback(
            "Recherche France entière — 300 villes à scanner...".format(len(points)),
            0.05,
        )

    results = _multi_search(
        session, query, points, progress_callback,
        progress_start=0.05, progress_end=0.85,
    )

    if progress_callback:
        progress_callback(
            "{} entreprises trouvées sur toute la France".format(len(results)),
            0.85,
        )

    return results if results else None


# ─────────────────────────────────────────────────────────────────────────────
# Méthode 2 : Selenium (fallback lent, ~60 secondes)
# ─────────────────────────────────────────────────────────────────────────────

def _detect_google_error(driver):
    """Détecte si Google bloque l'accès."""
    page_source = driver.page_source.lower()
    current_url = driver.current_url.lower()

    if "recaptcha" in page_source or "captcha" in page_source:
        return (
            "Google a détecté un usage automatisé et demande un CAPTCHA. "
            "Attendez quelques minutes avant de relancer une recherche."
        )
    if "unusual traffic" in page_source or "trafic inhabituel" in page_source:
        return (
            "Google a détecté un trafic inhabituel depuis votre connexion. "
            "Attendez 5-10 minutes avant de réessayer."
        )
    if "sorry" in current_url and "google" in current_url:
        return (
            "Google a temporairement bloqué les requêtes. "
            "Réessayez dans quelques minutes."
        )
    return None


def _create_driver():
    """Crée un driver Chrome headless."""
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def _scrape_via_selenium(query, geo_lat, geo_lng, max_results,
                         progress_callback, error_callback):
    """Scrape Google Maps via Selenium headless (fallback lent).

    Retourne une liste de dicts ou une liste vide.
    """
    if geo_lat is not None and geo_lng is not None:
        url = (
            "https://www.google.com/maps/search/{q}/@{lat},{lng},13z"
        ).format(q=quote_plus(query), lat=geo_lat, lng=geo_lng)
    else:
        url = "https://www.google.com/maps/search/{q}".format(q=quote_plus(query))

    if progress_callback:
        progress_callback("Lancement du navigateur (méthode alternative)...", 0.05)

    driver = _create_driver()
    results = []

    try:
        if progress_callback:
            progress_callback("Chargement de Google Maps...", 0.1)

        max_retries = 2
        google_error = None

        for attempt in range(max_retries + 1):
            driver.get(url)
            time.sleep(3 + attempt * 2)

            try:
                accept_btn = driver.find_element(
                    By.XPATH,
                    "//button[contains(., 'Tout accepter') or contains(., 'Accept all')]",
                )
                accept_btn.click()
                time.sleep(2)
            except Exception:
                pass

            google_error = _detect_google_error(driver)
            if google_error:
                if attempt < max_retries:
                    if progress_callback:
                        progress_callback(
                            "Nouvelle tentative ({}/{})...".format(
                                attempt + 2, max_retries + 1
                            ),
                            0.1,
                        )
                    time.sleep(5 + attempt * 5)
                    continue
                else:
                    if error_callback:
                        error_callback(google_error)
                    return results
            break

        if progress_callback:
            progress_callback("Recherche des entreprises...", 0.2)

        try:
            scrollable = driver.find_element(By.CSS_SELECTOR, 'div[role="feed"]')
        except Exception:
            try:
                scrollable = driver.find_element(
                    By.CSS_SELECTOR,
                    'div[aria-label*="Résultats"], div[aria-label*="Results"]',
                )
            except Exception:
                error = _detect_google_error(driver)
                if error and error_callback:
                    error_callback(error)
                elif error_callback:
                    error_callback(
                        "Impossible de charger les résultats Google Maps. "
                        "La page n'a pas le format attendu."
                    )
                return results

        max_scrolls = max(5, (max_results // 7) + 3)
        prev_dom_count = 0
        seen_names = set()
        place_urls = []
        feed_ratings = {}

        _JS_CARD_TEXT = (
            "var el = arguments[0];"
            "while (el.parentElement && "
            "el.parentElement.getAttribute('role') !== 'feed') "
            "{ el = el.parentElement; }"
            "return el.innerText;"
        )
        _RATING_RE = re.compile(
            r"(\d[,.]\d)[^(]{0,30}\((\d[\d\s\u202f\xa0]*)\)"
        )

        def _collect_visible_cards():
            links = driver.find_elements(
                By.CSS_SELECTOR, 'a[href*="/maps/place/"]'
            )
            for link in links:
                try:
                    name = link.get_attribute("aria-label")
                    href = link.get_attribute("href")
                    if not name or not href or name in seen_names:
                        continue
                    seen_names.add(name)
                    place_urls.append((name, href))
                    try:
                        card_text = driver.execute_script(_JS_CARD_TEXT, link)
                        if card_text:
                            m = _RATING_RE.search(card_text)
                            if m:
                                feed_ratings[name] = {
                                    "note": m.group(1).replace(",", "."),
                                    "nb_avis": int(re.sub(r"\D", "", m.group(2))),
                                }
                    except Exception:
                        pass
                except Exception:
                    continue
            return len(links)

        for _i in range(max_scrolls):
            driver.execute_script(
                "arguments[0].scrollTop = arguments[0].scrollHeight", scrollable
            )
            time.sleep(1.5)
            dom_count = _collect_visible_cards()

            if progress_callback:
                progress_callback(
                    "Chargement... ({} trouvés)".format(len(place_urls)),
                    0.2 + 0.45 * min(len(place_urls) / max_results, 1.0),
                )

            if len(place_urls) >= max_results:
                break
            if dom_count == prev_dom_count:
                time.sleep(1.5)
                driver.execute_script(
                    "arguments[0].scrollTop = arguments[0].scrollHeight",
                    scrollable,
                )
                time.sleep(1.5)
                dom_count = _collect_visible_cards()
                if dom_count == prev_dom_count:
                    break
            prev_dom_count = dom_count

        if not place_urls:
            items = driver.find_elements(
                By.CSS_SELECTOR, 'div[role="feed"] > div > div > a'
            )
            for item in items:
                try:
                    name = item.get_attribute("aria-label")
                    href = item.get_attribute("href")
                    if name and href and name not in seen_names:
                        seen_names.add(name)
                        place_urls.append((name, href))
                except Exception:
                    continue

        place_urls = place_urls[:max_results]

        # Compléter les notes manquantes depuis le texte du feed
        missing = [p for p, _ in place_urls if p not in feed_ratings]
        if missing:
            try:
                feed_text = scrollable.text
                for pname in missing:
                    idx = feed_text.find(pname)
                    if idx >= 0:
                        after = feed_text[idx + len(pname):idx + len(pname) + 80]
                        m = _RATING_RE.search(after)
                        if m:
                            feed_ratings[pname] = {
                                "note": m.group(1).replace(",", "."),
                                "nb_avis": int(re.sub(r"\D", "", m.group(2))),
                            }
            except Exception:
                pass

        if progress_callback:
            progress_callback(
                "Extraction des détails de {} entreprises...".format(len(place_urls)),
                0.75,
            )

        for idx, (name, place_url) in enumerate(place_urls):
            if progress_callback:
                progress_callback(
                    "Extraction {}/{} : {}".format(idx + 1, len(place_urls), name),
                    0.75 + 0.25 * (idx / len(place_urls)),
                )
            try:
                driver.get(place_url)
                time.sleep(2)

                info = {
                    "nom": name,
                    "adresse": "",
                    "telephone": "",
                    "telephone_raw": "",
                    "site_web": "",
                    "note": "",
                    "nb_avis": 0,
                }

                if name in feed_ratings:
                    info["note"] = feed_ratings[name]["note"]
                    info["nb_avis"] = feed_ratings[name]["nb_avis"]

                buttons = driver.find_elements(
                    By.CSS_SELECTOR, 'button[data-item-id]'
                )
                for btn in buttons:
                    try:
                        data_id = btn.get_attribute("data-item-id")
                        text = btn.get_attribute("aria-label") or btn.text
                        if data_id and "address" in data_id:
                            addr = re.sub(
                                r"^Adresse\s*:\s*", "", text, flags=re.IGNORECASE
                            ).strip()
                            info["adresse"] = re.sub(
                                r"^Address\s*:\s*", "", addr, flags=re.IGNORECASE
                            ).strip()
                        elif data_id and "phone" in data_id:
                            phone = re.sub(
                                r"^T[ée]l[ée]phone\s*:\s*", "", text, flags=re.IGNORECASE
                            ).strip()
                            info["telephone"] = re.sub(
                                r"^Phone\s*:\s*", "", phone, flags=re.IGNORECASE
                            ).strip()
                    except Exception:
                        continue

                try:
                    website_el = driver.find_element(
                        By.CSS_SELECTOR, 'a[data-item-id="authority"]'
                    )
                    info["site_web"] = website_el.get_attribute("href") or ""
                except Exception:
                    pass

                if not info["telephone"]:
                    try:
                        page_text = driver.find_element(By.TAG_NAME, "body").text
                        phone_match = re.search(
                            r'(?:0[1-9][\s.]?\d{2}[\s.]?\d{2}[\s.]?\d{2}[\s.]?\d{2}'
                            r'|\+33[\s.]?\d[\s.]?\d{2}[\s.]?\d{2}[\s.]?\d{2}[\s.]?\d{2})',
                            page_text,
                        )
                        if phone_match:
                            info["telephone"] = phone_match.group(0)
                    except Exception:
                        pass

                info["telephone_raw"] = info["telephone"]
                info["telephone"] = normalize_phone(info["telephone"])

                # Extraire la note depuis la fiche si manquante
                if not info["note"]:
                    try:
                        star_els = driver.find_elements(
                            By.CSS_SELECTOR, 'span[role="img"]'
                        )
                        for el in star_els:
                            label = el.get_attribute("aria-label") or ""
                            if "toile" in label.lower() or "star" in label.lower():
                                m = re.search(r"(\d[,.]\d)", label)
                                if m:
                                    info["note"] = m.group(1).replace(",", ".")
                                break
                    except Exception:
                        pass

                results.append(info)

            except Exception:
                results.append({
                    "nom": name, "adresse": "", "telephone": "",
                    "telephone_raw": "", "site_web": "", "note": "", "nb_avis": 0,
                })

    finally:
        driver.quit()

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Point d'entrée principal
# ─────────────────────────────────────────────────────────────────────────────

def scrape_google_maps(
    activite,
    zone,
    max_results=20,
    note_minimum=0.0,
    nb_avis_minimum=0,
    telephone_requis=False,
    portable_uniquement=False,
    site_web_requis=False,
    code_postal="",
    geo_lat=None,
    geo_lng=None,
    mode="simple",
    progress_callback=None,
    error_callback=None,
):
    """Scrape Google Maps pour trouver des entreprises.

    Modes de recherche :
        "simple"       — 1 requête HTTP (~2s)
        "approfondie"  — grille autour de la zone (~15s, ~500 résultats)
        "france"       — grille France entière (~3-5 min, ~5000+ résultats)

    Returns:
        Liste de dicts avec clés: nom, adresse, telephone, site_web, note, nb_avis
    """
    # Pour le mode France, la query = juste l'activité (pas de ville)
    if mode == "france":
        query = activite
    else:
        query = "{} {}".format(activite, zone)

    results = None

    # --- Méthode 1 : HTTP direct (rapide) ---
    if mode == "france":
        results = _scrape_via_http(
            query, 46.6, 2.2, max_results,
            mode="france",
            progress_callback=progress_callback,
        )
    elif geo_lat is not None and geo_lng is not None:
        results = _scrape_via_http(
            query, geo_lat, geo_lng, max_results,
            mode=mode,
            progress_callback=progress_callback,
        )

    # --- Méthode 2 : Selenium (fallback, sauf mode France) ---
    if not results and mode != "france":
        results = _scrape_via_selenium(
            query, geo_lat, geo_lng, max_results,
            progress_callback, error_callback,
        )

    if not results:
        results = []

    # --- Filtres de post-traitement ---
    if note_minimum > 0:
        results = [
            r for r in results
            if _parse_note(r.get("note", "")) >= note_minimum
        ]

    if nb_avis_minimum > 0:
        results = [r for r in results if r.get("nb_avis", 0) >= nb_avis_minimum]

    if telephone_requis:
        results = [r for r in results if r.get("telephone")]

    if portable_uniquement:
        _mobile_re = re.compile(r'(?:0[67]|\+33\s*[67])')
        results = [
            r for r in results
            if r.get("telephone") and _mobile_re.search(r["telephone"])
        ]

    if site_web_requis:
        results = [r for r in results if r.get("site_web")]

    # Filtrer par zone (sauf mode France)
    if mode != "france":
        results = _filter_by_zone(results, zone, code_postal)

    # Troncature au max_results uniquement en mode simple
    # (les modes approfondie/france sont faits pour maximiser les résultats)
    if mode == "simple" and len(results) > max_results:
        results = results[:max_results]

    if progress_callback:
        progress_callback(
            "Terminé ! {} résultats après filtrage.".format(len(results)), 1.0
        )

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Filtre géographique
# ─────────────────────────────────────────────────────────────────────────────

_GRANDES_VILLES = [
    "paris", "marseille", "lyon", "toulouse", "nice", "nantes", "strasbourg",
    "montpellier", "bordeaux", "lille", "rennes", "reims", "toulon", "grenoble",
    "dijon", "angers", "nîmes", "nimes", "villeurbanne", "clermont-ferrand",
    "le havre", "aix-en-provence", "brest", "limoges", "tours", "amiens",
    "perpignan", "metz", "besançon", "besancon", "orléans", "orleans",
    "rouen", "mulhouse", "caen", "nancy", "argenteuil", "montreuil",
    "saint-denis", "saint-étienne", "saint-etienne", "roubaix", "tourcoing",
    "avignon", "dunkerque", "poitiers", "pau", "calais", "la rochelle",
    "colmar", "valence",
]

_POSTAL_RE = re.compile(r"\b(\d{5})\b")


def _dept_from_postal(postal):
    """Extrait le code département d'un code postal."""
    if postal.startswith("97"):
        return postal[:3]
    return postal[:2]


def _filter_by_zone(results, zone, code_postal=""):
    """Filtre les résultats hors zone géographique."""
    if not zone and not code_postal:
        return results

    if code_postal:
        target_dept = _dept_from_postal(code_postal)
        zone_lower = zone.strip().lower() if zone else ""
        zone_kw = [w for w in zone_lower.split() if len(w) >= 3]
        if zone_kw:
            autres_villes = [
                v for v in _GRANDES_VILLES if not any(k in v for k in zone_kw)
            ]
        else:
            autres_villes = list(_GRANDES_VILLES)

        filtered = []
        for r in results:
            adresse = r.get("adresse", "")
            if not adresse:
                filtered.append(r)
                continue
            m = _POSTAL_RE.search(adresse)
            if m:
                if _dept_from_postal(m.group(1)) == target_dept:
                    filtered.append(r)
            else:
                adresse_low = adresse.lower()
                if any(v in adresse_low for v in autres_villes):
                    continue
                filtered.append(r)
        return filtered

    if not zone:
        return results

    zone_lower = zone.strip().lower()
    zone_keywords = [w for w in zone_lower.split() if len(w) >= 3]
    if not zone_keywords:
        zone_keywords = [zone_lower]

    autres_villes = [
        v for v in _GRANDES_VILLES
        if not any(kw in v for kw in zone_keywords)
    ]

    expected_depts = set()
    for r in results:
        adresse_low = r.get("adresse", "").lower()
        if any(kw in adresse_low for kw in zone_keywords):
            m = _POSTAL_RE.search(r.get("adresse", ""))
            if m:
                expected_depts.add(m.group(1)[:2])

    filtered = []
    for r in results:
        adresse = r.get("adresse", "")
        adresse_low = adresse.lower()
        if not adresse_low:
            filtered.append(r)
            continue
        if any(kw in adresse_low for kw in zone_keywords):
            filtered.append(r)
            continue
        if any(v in adresse_low for v in autres_villes):
            continue
        if expected_depts:
            m = _POSTAL_RE.search(adresse)
            if m and m.group(1)[:2] not in expected_depts:
                continue
        filtered.append(r)

    return filtered


def _parse_note(note_str):
    """Convertit une note string en float."""
    try:
        return float(note_str.replace(",", "."))
    except (ValueError, AttributeError):
        return 0.0
