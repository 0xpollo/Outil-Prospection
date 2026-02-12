# Outil de Prospection — Nexoflow Studio

Outil interne pour trouver les coordonnées d'entreprises (téléphone, email, site web) à partir d'un domaine d'activité et d'une zone géographique. Les données sont extraites via Google Maps.

## Fonctionnalités

- Recherche d'entreprises par activité + zone géographique
- Extraction automatique : nom, adresse, téléphone, site web, note Google, nombre d'avis
- Enrichissement email (scraping des sites web trouvés)
- Filtres de recherche : note minimum, nombre d'avis minimum, avec téléphone/site web/email uniquement
- Export des résultats en CSV ou Excel

## Installation

```bash
pip install -r requirements.txt
```

## Lancement

```bash
streamlit run app.py
```

## Stack technique

- **Interface** : Streamlit
- **Scraping Google Maps** : Selenium (Chrome headless)
- **Extraction emails** : Requests + BeautifulSoup + regex
- **Export** : Pandas + openpyxl
