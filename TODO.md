# TODO post-port enrichment (cf. ENRICHMENT_REPORT.md)

Choses repérées pendant le port, **pas implémentées** pour rester focus :

- [ ] Brancher `advanced_enrichment.enrich_advanced` dans `app.py` (toggle UI + colonnes export Excel/Airtable). Voir section "Comment intégrer dans app.py" du rapport.
- [ ] Configurer `PERPLEXITY_API_KEY` dans `.env` (cf. `BLOCKERS.md`).
- [ ] Décider si on garde `email_finder.py` (logique nominale historique) ou si on bascule entièrement sur `advanced_enrichment.find_dirigeant_email` qui couvre les mêmes cas avec un pipeline plus robuste (variantes de nom, fallback Perplexity).
- [ ] Ajouter `python-dotenv` à `requirements.txt` si on veut un parsing `.env` standardisé (actuellement parser maison léger dans `config.py`).
- [ ] Compléter `_NAME_STOP_WORDS` à mesure qu'on observe des faux positifs (TODO laissé en commentaire dans `validators.py`).
