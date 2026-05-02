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
                                                     (filtre PLACEHOLDER_DOMAINS + decode \uXXXX)
  5. email_finder.validate_scraped_emails()        → SMTP RCPT TO via VPS ;
                                                     drop l'email si invalid/no_mx (status conservé)
  6. email_finder.enrich_nominative_emails()       → patterns + SMTP (si advanced OFF, aujourd'hui
                                                     jamais déclenché depuis l'UI)

PHASE 2 — Perplexity, sur les MANQUANTES uniquement (jusqu'à advanced_max)
  7. advanced_enrichment.enrich_advanced()         → Perplexity site + dirigeant + stratégique
                                                     (PARALLÈLE : ThreadPoolExecutor 6 workers)

  → save_search()  (filtre email_requis appliqué APRÈS, pas pendant)
```

**Principe inviolable** : aucune étape ne supprime d'entreprise. Asserts dans `pipeline.py` qui crashent le worker sinon. Les filtres (`email_requis`, etc.) sont appliqués UNIQUEMENT à la sauvegarde finale, jamais pendant l'enrichissement. **Exception contrôlée** : un email scrapé que SMTP confirme `invalid`/`no_mx`/`invalid_format` est vidé (mais l'entreprise reste, et le `email_status` est conservé pour traçabilité ; Phase 2 firera dessus puisque `_has_quality_email` retourne False).

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
| `advanced_enrichment.py` | `find_dirigeant_email`, `find_strategic_emails`, orchestration Phase 2 (parallèle) |
| `email_enricher.py` | Scraping HTML des sites + filtres `PLACEHOLDER_DOMAINS` + decode JSON unicode |
| `email_finder.py` | Patterns dirigeant + validation SMTP (délègue à `smtp_verifier`, plus de fallback local) |
| `entreprise_enricher.py` | API recherche-entreprises (data.gouv.fr) → dirigeant |
| `scoring.py` | Scoring 0-100 calibré sur la qualité du meilleur email (cf. § Scoring) |
| `database.py` | SQLite : `searches`, `entreprises`, `recherche_entreprises`, `jobs` ; sérialise `emails_strategiques` en JSON |
| `revalidate_dirigeants.py` | Rattrapage : retest SMTP des `email_dirigeant` à confiance vide. Usage : `python3 revalidate_dirigeants.py [--search-id N]` |
| `revalidate_all_emails.py` | Rattrapage : retest des emails scrapés (jamais testés + `--retry-unknown`). Skip auto des perso. |

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
- **`entreprises`** : 1 ligne par couple (nom, adresse) unique, partagée entre searches.
  Colonnes liées aux emails : `emails`, `email_status`, `email_confidence`, `emails_confiance`,
  `emails_strategiques` (JSON), `email_dirigeant`, `email_dirigeant_confidence`.
  Migration auto au démarrage (`init_db` ALTER TABLE pour bases existantes).
- **`recherche_entreprises`** : table de liaison + flag `deja_connue`
- **`jobs`** : queue d'exécution (status, progress, message, stats_json)
  - `pending` → `running` (claim atomic) → `done`/`failed`/`cancelled`

**Lecture défensive** : `pick_best_email` et `scoring._best_email_signals` acceptent `emails_strategiques` aussi bien comme liste in-memory que comme string JSON (au cas où un script lit la DB sans passer par `get_search_results` qui désérialise).

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
   `dirigeant` > `direction/cabinet` > `RH` > `site` > `contact` > `perso`

**Source `perso`** : email scrapé sur un domaine public (gmail/outlook/yahoo/…). Le MX accepte tout, donc impossible à valider via SMTP RCPT. Toujours classé `incertain` (Froid). Détection : `domain in PUBLIC_EMAIL_DOMAINS` ou `email_status == "public"`.

L'UI affiche **1 seule colonne Email** avec badge confiance + suffixe source.

## Points techniques critiques

- **Pipeline ne supprime jamais d'entreprise** : asserts dans pipeline.py. Seule exception : un email scrapé confirmé `invalid`/`no_mx` est vidé (l'entreprise reste).
- **Validation des sites en amont** : rejette renault.fr pour BERTHIAND AUTOMOBILES (groupe parent), pagesjaunes.fr (annuaire), et les sites où aucun token du nom n'apparaît dans le slug ni la page
- **Filtres anti-bruit côté scraping** :
  - `PLACEHOLDER_DOMAINS` (email_enricher.py) : rejette `@domain.com`, `@exemple.com`, `@local.fr`, `@webador.fr`, `@mapszi.com`, `@centralapp.com`… (placeholders + plateformes de site)
  - `_decode_json_escapes` : décode les `>` et autres artefacts JSON dans le HTML scrapé (sinon `>info@x.com` matchait la regex email)
- **Phase 2 sélective** : Perplexity ne tourne que sur les entreprises sans email vérifié+probable, économise ~30-70% de tokens
- **Phase 2 parallèle** : `enrich_advanced` utilise `ThreadPoolExecutor` 6 workers (Perplexity + SMTP IO-bound). 100 entreprises ≈ 4-5 min vs ~25 min en série.
- **SMTP "unknown" → "probable"** : sur OVH/IONOS qui répondent ambigument au RCPT, on classe en "probable" (cf. spec utilisateur, pas de Debounce en fallback)
- **`smtp_verifier.is_available()` cache TTL** : True caché en permanence, False caché 60s (évite de payer 15s × 6 calls/ent × 100 ent quand le service est HS, mais retest auto après expiration).
- **`find_dirigeant_email` ne gate PLUS sur `is_available`** : appelle directement `check_domain`/`verify_email` ; si le service répond `error`/`unknown`, le fallback intelligent met `probable` (MX présent) ou `incertain` (sinon).
- **Email Perplexity validé SMTP** : quand Perplexity trouve un email, `verify_email` est appelé derrière pour poser la bonne confiance (sinon il atterrissait en `incertain` → D Froid).
- **Filtre `email_requis`** : appliqué à la sauvegarde finale uniquement, jamais pendant le pipeline
- **Mode ultra query** : sans nom de ville (cf. gotcha ci-dessus)
- **Selenium fallback** : actif uniquement en mode `simple`/`approfondie`. En `ultra`/`france`, le HTTP a déjà tenté ; pas de scroll Selenium 5-8 min pour rien.
- **Selenium VPS** : pose les cookies de consent FR (sinon page allemande sur VPS Contabo)
- **FR/EN persistant** : Phase 2 écrit `email_dirigeant_confiance` (FR) et `emails_confiance` (FR), Phase 1 écrit `_confidence` (EN). Lecture toujours défensive (`get("X_confiance") or get("X_confidence")`) — ne pas tenter de normaliser, c'est compensé.

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

## Scoring des prospects

Calibré uniquement sur la qualité du meilleur email disponible (matrice source × confiance, cf. `_best_email_signals` + `calculate_score`). Aucun signal annexe (téléphone, taille, note Google) — seul l'email compte.

| Score | Tier | Couleur | Critère |
|-------|------|---------|---------|
| 90 | A — Hot | vert (`#16a34a`) | nominatif **dirigeant** + **vérifié** SMTP |
| 70 | B — Chaud | vert clair (`#84cc16`) | (dirigeant + probable) **OU** (direction/RH/métier + vérifié) |
| 40 | C — Tiède | orange (`#f59e0b`) | dirigeant incertain/non testé, direction probable, ou générique vérifié |
| 10 | D — Froid | gris (`#94a3b8`) | générique probable/incertain, ou aucun email |
| 0  | Exclu | gris foncé | franchise / groupe / chaîne (`_FRANCHISE_KEYWORDS`) |

`score_label` retourne `Hot / Chaud / Tiède / Froid / Exclu`.

## Export Excel (format `sortie.xlsx`)

Une seule sortie Excel (Airtable retiré). Format strict :
- **Sheet1** (14 col, A→N) : Nom | Adresse | Téléphone | Email | Source Email | Qualité Email | Site Web | Dirigeant | Statut | Date d'envoi | Nom nettoyé | Ville | Note | Date ISO (formule, réf col J). `Note` = tier `A — Hot (90)` / `B — Chaud (70)` / `C — Tiède (40)` / `D — Froid (10)` / `Exclu`.
- **Envoi**, **Relance1**, **Relance2** : en-têtes `Destinataire | Date et Heure | Objet | Corp` (suivi manuel)

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
