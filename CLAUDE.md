# Outil de Prospection - Nexoflow Studio

## Description
Outil de prospection B2B pour trouver des prospects via Google Maps et leur vendre des services d'automatisation (Make, n8n) et d'intégration IA. Scraping Selenium (pas d'API Google Maps).

## Stack technique
- **Python 3.9** (version système macOS, utilisée par Streamlit)
- Streamlit (UI), Selenium headless Chrome (scraping), SQLite (historique), BeautifulSoup (emails)
- Dépendances : voir `requirements.txt`
- **Pas de syntaxe Python 3.10+** (`str | None`, `match/case` interdits)

## Fichiers principaux
| Fichier | Rôle |
|---------|------|
| `app.py` | Interface Streamlit — 2 onglets (Recherche / Historique), exports Excel + Airtable |
| `scraper.py` | Scraping Google Maps via Selenium headless Chrome |
| `email_enricher.py` | Extraction emails depuis les sites web des entreprises |
| `scoring.py` | Scoring de qualification prospect (0-100) avec détection franchises |
| `database.py` | SQLite — historique recherches, déduplication entreprises |

## Pipeline de données
```
scraper.scrape_google_maps() → list[dict]
    ↓
email_enricher.enrich_emails() → ajoute 'emails'
    ↓
scoring.calculate_scores() → ajoute 'score'
    ↓
database.save_search() → persiste + détecte 'deja_connue'
    ↓
app.py → affichage + export
```

## Points techniques critiques
- **Note/nb_avis** : extraits depuis le texte du `div[role="feed"]` (la liste de résultats), pas depuis les fiches individuelles (Google Maps lazy-load le `(NNN)` de manière intermittente en headless)
- **Filtre zone** : exclut par nom de grande ville + vérification code postal (département)
- **Scoring** : franchises éliminées (score=0), priorité à l'accessibilité du décisionnaire (portable, email nominatif)
- **Tableau HTML** : colonnes Note/Nb avis/Score triables par clic (JavaScript client-side)

## Lancer le projet
```bash
/usr/bin/python3 -m streamlit run app.py
```

## Design
- Branding Nexoflow Studio
- Couleur principale : `#0EA5E9` (bleu)
- Couleur secondaire : `#B8C4E0` (bleu lavande)
- Interface en français
