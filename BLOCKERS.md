# Blockers rencontrés pendant le port

## PERPLEXITY_API_KEY manquante

Au moment des tests, la variable d'env `PERPLEXITY_API_KEY` n'était pas configurée
(ni dans le shell, ni dans un `.env`). Conséquence : **l'étape Perplexity Sonar a
été désactivée** pour tous les runs de test (flag `--no-perplexity`).

L'impact réel est masqué sur "expert-comptable Bordeaux" (85%) car la plupart des
cabinets ont déjà un site sur Google Maps. En revanche sur "garagiste Marseille"
(25%), beaucoup d'entreprises n'ont pas de site dans le scraping GMaps initial,
et Perplexity aurait pu en trouver — d'où la chute de qualité.

**Pour activer la chaîne complète** :
1. Copier `.env.example` vers `.env`
2. Remplir `PERPLEXITY_API_KEY=...` (récupérer sur perplexity.ai/api)
3. Relancer `python3 run_test.py --limit 20`

Avec Perplexity actif, on attend ~+10 à +20 points de qualité sur les requêtes
où les entreprises sont peu indexées (artisans, garages).
