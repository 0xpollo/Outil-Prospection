# Outil de Prospection - Nexoflow Studio

## Description
Outil de prospection B2B pour trouver des prospects via Google Maps et leur vendre des services d'automatisation (Make, n8n) et d'intégration IA. Scraping HTTP direct (Selenium en fallback uniquement). Pipeline en 2 phases : enrichissement gratuit puis Perplexity sélective.

## Stack technique
- **Python 3.9** local (système macOS) / **3.12** sur le VPS
- Streamlit (UI), requests (HTTP scraping), Selenium (fallback Chrome), SQLite, BeautifulSoup
- Perplexity Sonar (recherche site/email dirigeant), service VPS Contabo (validation SMTP RCPT TO)
- Pas de syntaxe Python 3.10+ (`str | None`, `match/case` interdits)

## Architecture (2 phases — JAMAIS perdre une entreprise)

```
PHASE 1 — gratuite, sur TOUTES les entreprises
  1. scraper.scrape_google_maps()                  → list de dicts
  2. pipeline._validate_initial_sites()            → rejette annuaires + groupes parents
  3. entreprise_enricher.enrich_entreprises()      → API gouv → dirigeant
  4. email_enricher.enrich_emails()                → scraping HTML sites validés
  5. email_finder.validate_scraped_emails()        → SMTP RCPT TO via VPS
  6. email_finder.enrich_nominative_emails()       → patterns + SMTP (si advanced OFF)

PHASE 2 — Perplexity, sur les MANQUANTES uniquement (jusqu'à advanced_max)
  7. advanced_enrichment.enrich_advanced()         → Perplexity site + dirigeant + stratégique

  → save_search()  (filtre email_requis appliqué APRÈS, pas pendant)
```

**Principe inviolable** : aucune étape ne supprime d'entreprise. Asserts dans `pipeline.py` qui crashent le worker sinon. Les filtres (`email_requis`, etc.) sont appliqués UNIQUEMENT à la sauvegarde finale, jamais pendant l'enrichissement.

## Fichiers principaux

| Fichier | Rôle |
|---------|------|
| `app.py` | UI Streamlit (3 onglets : Recherche / Jobs / Historique) |
| `pipeline.py` | Orchestrateur 2-phase, asserts anti-perte |
| `worker.py` | Process séparé qui exécute les jobs depuis la queue DB |
| `scraper.py` | Scraping Google Maps HTTP + fallback Selenium |
| `validators.py` | `_NAME_STOP_WORDS`, `validate_site_matches_company`, `is_parent_group_site`, blacklists annuaires |
| `perplexity_search.py` | Wrapper Perplexity Sonar (site officiel + dirigeant) |
| `smtp_verifier.py` | Wrapper du service VPS de validation SMTP RCPT TO |
| `gmaps_lookup.py` | Fallback GMaps via scraper local |
| `advanced_enrichment.py` | `find_dirigeant_email`, `find_strategic_emails`, orchestration Phase 2 |
| `email_enricher.py` | Scraping HTML des sites pour extraire emails |
| `email_finder.py` | Patterns dirigeant + validation SMTP |
| `entreprise_enricher.py` | API recherche-entreprises (data.gouv.fr) → dirigeant |
| `scoring.py` | Scoring 0-100, franchises = 0 |
| `database.py` | SQLite : `searches`, `entreprises`, `recherche_entreprises`, `jobs` |

## Modes de scraping

| Mode | Temps | Résultats | Quand |
|------|-------|-----------|-------|
| `simple` | ~2 s | ~200 max | Test rapide |
| `approfondie` | ~30 s | ~400-500 | Standard, filtre par département |
| `ultra` | ~3-5 min | ~1000-4000 | Métropole entière (no filtre dept) |
| `france` | ~3-5 min | milliers | France entière, 300 communes |

**Gotcha critique mode ultra/france** : la query passée à Google = juste l'activité (`"restaurant"`), JAMAIS `"restaurant Lyon"`. Avec le nom de la ville, Google biaise vers le centre et ignore les coords GPS pour les banlieues : 921 résultats vs 4323 sur Lyon.

## Modèle de données

- **`searches`** : 1 ligne par recherche (id, activite, zone, date, params)
- **`entreprises`** : 1 ligne par couple (nom, adresse) unique, partagée entre searches
- **`recherche_entreprises`** : table de liaison + flag `deja_connue`
- **`jobs`** : queue d'exécution (status, progress, message, stats_json)
  - `pending` → `running` (claim atomic) → `done`/`failed`/`cancelled`

## Configuration `.env`

```
VERIFIER_URL=http://79.143.189.160:8000   # service VPS SMTP (port 8000)
VERIFIER_KEY=...                          # défaut hardcodé dans config.py
PERPLEXITY_API_KEY=pplx-xxx               # obligatoire pour mode avancé
```

`_load_dotenv` ignore les valeurs vides (sinon `.env.example` écrasait la clé hardcodée).

## Pipeline de jobs (UI ↔ worker)

```
UI (app.py)              DB (jobs table)            worker.py
─────────────            ─────────────              ─────────────
create_job()  ──────────→ status='pending'
                                ↓
                          claim_next_job()  ──────→ status='running'
                                ↓
                          update_job_progress()  ←── progress_callback
                          (toutes les 1 s)
                                ↓
                          finish_job()  ←─────────── pipeline.run_search()
                          status='done' + stats
                                ↑
list_jobs() ←──────────── (auto-refresh 3 s en UI)
                                ↑
cancel_job()  ──────────→ status='cancelled'
                          (worker check toutes les 2 s, sort proprement)
```

## Sélection de l'email pour la prospection

`app.pick_best_email(entreprise)` choisit parmi `email_dirigeant`, `emails` (scrapé), `emails_strategiques` :
1. **Confiance d'abord** : `vérifié` > `probable` > `incertain`
2. **À confiance égale, source par priorité** :
   `dirigeant` > `direction/cabinet` > `RH` > `site` (contact@) > `contact générique`

L'UI affiche **1 seule colonne Email** avec badge confiance + suffixe source.

## Points techniques critiques

- **Pipeline ne supprime jamais** : asserts dans pipeline.py
- **Validation des sites en amont** : rejette renault.fr pour BERTHIAND AUTOMOBILES (groupe parent), pagesjaunes.fr (annuaire), et les sites où aucun token du nom n'apparaît dans le slug ni la page
- **Phase 2 sélective** : Perplexity ne tourne que sur les entreprises sans email vérifié+probable, économise ~30-70% de tokens
- **SMTP "unknown" → "probable"** : sur OVH/IONOS qui répondent ambigument au RCPT, on classe en "probable" (cf. spec utilisateur, pas de Debounce en fallback)
- **Filtre `email_requis`** : appliqué à la sauvegarde finale uniquement, jamais pendant le pipeline
- **Mode ultra query** : sans nom de ville (cf. gotcha ci-dessus)
- **Selenium VPS** : pose les cookies de consent FR (sinon page allemande sur VPS Contabo)

## Déploiement VPS

- **URL** : `http://79.143.189.160` (Basic Auth nginx, user `admin`)
- **Path** : `/opt/outil-prospection/` (venv Python 3.12)
- **Services systemd** :
  - `outil-prospection-web.service` (Streamlit sur 127.0.0.1:8501)
  - `outil-prospection-worker.service` (worker.py, restart=always)
- **Reverse proxy nginx** : Basic Auth + WebSocket pour Streamlit (`/etc/nginx/sites-available/outil-prospection`)
- **Logs** : `/var/log/outil-prospection-{web,worker}.log`
- **Skill push-vps** : `~/.claude/skills/push-vps/` pour déployer/redémarrer

Pour pousser une mise à jour :
```bash
rsync -avz pipeline.py app.py ... contabo:/opt/outil-prospection/
ssh contabo 'systemctl restart outil-prospection-web outil-prospection-worker'
```

## Export Excel (format `sortie.xlsx`)

Une seule sortie Excel (Airtable retiré). Format strict :
- **Sheet1** : Nom | Adresse | Téléphone | Email | Source | (vide) | Site Web | Statut | Date d'envoi | Nom nettoyé | Ville | Date ISO (formule)
- **Envoi**, **Relance1**, **Relance2** : feuilles de suivi vides pour usage manuel

## Lancer en local

```bash
# Worker (1er terminal)
/usr/bin/python3 worker.py

# UI (2e terminal)
/usr/bin/python3 -m streamlit run app.py
```

DB locale `prospection.db` séparée de celle du VPS — chaque environnement est indépendant.

## Design

- Branding Nexoflow Studio
- Couleur principale : `#0EA5E9` (bleu)
- Couleur secondaire : `#B8C4E0` (bleu lavande)
- Interface en français
- App macOS dans le Dock (`/Applications/Outil Prospection.app`) ouvre l'URL VPS

## Tests

```bash
/usr/bin/python3 -m unittest test_enrichment.py
```

17 tests : validation de site (BERTHIAND→renault.fr rejeté), variantes de nom (de la Fontaine, MARTIN-DUPONT, D'Artagnan), patterns email, blacklist annuaires, etc.
