"""Scraping Google Maps pour récupérer les coordonnées d'entreprises."""

import time
import re
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager


def create_driver():
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


def scrape_google_maps(
    activite: str,
    zone: str,
    max_results: int = 20,
    note_minimum: float = 0.0,
    nb_avis_minimum: int = 0,
    telephone_requis: bool = False,
    site_web_requis: bool = False,
    progress_callback=None,
):
    """
    Scrape Google Maps pour trouver des entreprises.

    Args:
        activite: Domaine d'activité (ex: "Restaurant")
        zone: Zone géographique (ex: "Lyon")
        max_results: Nombre maximum de résultats
        note_minimum: Note Google minimum (0-5)
        nb_avis_minimum: Nombre d'avis Google minimum
        telephone_requis: Ne garder que les résultats avec téléphone
        site_web_requis: Ne garder que les résultats avec site web
        progress_callback: Fonction appelée avec (message, progression 0-1)

    Returns:
        Liste de dicts avec clés: nom, adresse, telephone, site_web, note, nb_avis
    """
    query = f"{activite} à {zone}"
    url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"

    if progress_callback:
        progress_callback("Lancement du navigateur...", 0.05)

    driver = create_driver()
    results = []

    try:
        if progress_callback:
            progress_callback("Chargement de Google Maps...", 0.1)

        driver.get(url)
        time.sleep(3)

        # Accepter les cookies si le bouton apparaît
        try:
            accept_btn = driver.find_element(
                By.XPATH, "//button[contains(., 'Tout accepter') or contains(., 'Accept all')]"
            )
            accept_btn.click()
            time.sleep(2)
        except Exception:
            pass

        if progress_callback:
            progress_callback("Recherche des entreprises...", 0.2)

        # Scroller la liste des résultats pour en charger plus
        try:
            scrollable = driver.find_element(By.CSS_SELECTOR, 'div[role="feed"]')
        except Exception:
            # Essayer un autre sélecteur
            try:
                scrollable = driver.find_element(
                    By.CSS_SELECTOR, 'div[aria-label*="Résultats"], div[aria-label*="Results"]'
                )
            except Exception:
                if progress_callback:
                    progress_callback("Aucun résultat trouvé.", 1.0)
                return results

        # Scroller pour charger plus de résultats
        for i in range(5):
            driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight", scrollable)
            time.sleep(1.5)
            if progress_callback:
                progress_callback(f"Chargement des résultats... ({i+1}/5)", 0.2 + i * 0.1)

        if progress_callback:
            progress_callback("Extraction des fiches...", 0.7)

        # Récupérer les liens des fiches
        links = driver.find_elements(By.CSS_SELECTOR, 'a[href*="/maps/place/"]')
        seen_names = set()
        place_urls = []

        for link in links:
            try:
                name = link.get_attribute("aria-label")
                href = link.get_attribute("href")
                if name and href and name not in seen_names:
                    seen_names.add(name)
                    place_urls.append((name, href))
            except Exception:
                continue

        if not place_urls:
            # Approche alternative : chercher les éléments de la liste
            items = driver.find_elements(By.CSS_SELECTOR, 'div[role="feed"] > div > div > a')
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

        if progress_callback:
            progress_callback(f"Extraction des détails de {len(place_urls)} entreprises...", 0.75)

        # Visiter chaque fiche pour extraire les détails
        for idx, (name, place_url) in enumerate(place_urls):
            if progress_callback:
                progress_callback(
                    f"Extraction {idx+1}/{len(place_urls)} : {name}",
                    0.75 + 0.25 * (idx / len(place_urls))
                )

            try:
                driver.get(place_url)
                time.sleep(2)

                info = {
                    "nom": name,
                    "adresse": "",
                    "telephone": "",
                    "site_web": "",
                    "note": "",
                    "nb_avis": 0,
                }

                # Extraire la note
                try:
                    rating_el = driver.find_element(By.CSS_SELECTOR, 'div[role="img"][aria-label*="étoile"], div[role="img"][aria-label*="star"]')
                    rating_text = rating_el.get_attribute("aria-label")
                    rating_match = re.search(r"([\d,\.]+)", rating_text)
                    if rating_match:
                        info["note"] = rating_match.group(1).replace(",", ".")
                except Exception:
                    pass

                # Extraire le nombre d'avis
                try:
                    reviews_el = driver.find_element(By.CSS_SELECTOR, 'button[jsaction*="review"] span, button[aria-label*="avis"], button[aria-label*="review"]')
                    reviews_text = reviews_el.get_attribute("aria-label") or reviews_el.text
                    reviews_match = re.search(r"([\d\s]+)", reviews_text.replace("\u202f", "").replace("\xa0", ""))
                    if reviews_match:
                        info["nb_avis"] = int(reviews_match.group(1).replace(" ", ""))
                except Exception:
                    pass

                # Extraire adresse, téléphone, site web depuis les boutons d'info
                buttons = driver.find_elements(By.CSS_SELECTOR, 'button[data-item-id]')
                for btn in buttons:
                    try:
                        data_id = btn.get_attribute("data-item-id")
                        text = btn.get_attribute("aria-label") or btn.text

                        if data_id and "address" in data_id:
                            info["adresse"] = text.replace("Adresse\u202f: ", "").replace("Address: ", "").strip()
                        elif data_id and "phone" in data_id:
                            info["telephone"] = text.replace("Téléphone\u202f: ", "").replace("Phone: ", "").strip()
                    except Exception:
                        continue

                # Site web
                try:
                    website_el = driver.find_element(By.CSS_SELECTOR, 'a[data-item-id="authority"]')
                    info["site_web"] = website_el.get_attribute("href") or ""
                except Exception:
                    pass

                # Fallback : chercher dans le texte de la page
                if not info["telephone"]:
                    try:
                        page_text = driver.find_element(By.TAG_NAME, "body").text
                        phone_match = re.search(
                            r'(?:0[1-9][\s.]?\d{2}[\s.]?\d{2}[\s.]?\d{2}[\s.]?\d{2}|\+33[\s.]?\d[\s.]?\d{2}[\s.]?\d{2}[\s.]?\d{2}[\s.]?\d{2})',
                            page_text
                        )
                        if phone_match:
                            info["telephone"] = phone_match.group(0)
                    except Exception:
                        pass

                results.append(info)

            except Exception:
                results.append({"nom": name, "adresse": "", "telephone": "", "site_web": "", "note": ""})

    finally:
        driver.quit()

    # Filtres de post-traitement
    if note_minimum > 0:
        results = [
            r for r in results
            if _parse_note(r.get("note", "")) >= note_minimum
        ]

    if nb_avis_minimum > 0:
        results = [r for r in results if r.get("nb_avis", 0) >= nb_avis_minimum]

    if telephone_requis:
        results = [r for r in results if r.get("telephone")]

    if site_web_requis:
        results = [r for r in results if r.get("site_web")]

    if progress_callback:
        progress_callback(f"Terminé ! {len(results)} résultats après filtrage.", 1.0)

    return results


def _parse_note(note_str: str) -> float:
    """Convertit une note string en float."""
    try:
        return float(note_str.replace(",", "."))
    except (ValueError, AttributeError):
        return 0.0
