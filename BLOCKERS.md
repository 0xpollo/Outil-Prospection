# Blockers rencontrés pendant le port

> Tous résolus. Section gardée pour traçabilité.

## ✅ Résolu — PERPLEXITY_API_KEY manquante

Au moment du 1ᵉʳ run de test, la variable `PERPLEXITY_API_KEY` n'était pas configurée.
Le user a ajouté la clé dans `.env`, run relancé, **+8,4 points de qualité**.

## ✅ Résolu — `_load_dotenv` écrasait `VERIFIER_KEY` avec une chaîne vide

Le fichier `.env.example` contenait `VERIFIER_KEY=` (vide, pour que le user puisse
la remplir si besoin). Mon parser maison `_load_dotenv` (dans `config.py`) faisait
`os.environ[key] = value` même quand `value` était vide → écrasait la clé valide
hardcodée en défaut dans `config.py`.

Fix : `_load_dotenv` ignore désormais les valeurs vides. Le défaut `config.py`
reprend la main quand le `.env` n'a pas de valeur.

## ✅ Résolu — Timeout SMTP service trop court

`/catchall` côté serveur fait un RCPT TO réel sur le MX du domaine cible. Selon
le domaine, ça prend 6 à 10 s. Le timeout user-spec était 8 s → on coupait à la
limite et on perdait la majorité des check de domaine.

Fix : `_TIMEOUT = 20 s` pour `/catchall` et `/verify`, `_HEALTH_TIMEOUT = 5 s`
pour `/health`. Documenté dans `ENRICHMENT_REPORT.md`.
