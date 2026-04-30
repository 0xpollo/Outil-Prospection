"""Tests unitaires des modules d'enrichissement portés du bot France Travail.

Lancer : python3 -m unittest test_enrichment.py
"""

import unittest
from unittest.mock import patch, MagicMock

from validators import (
    ANNUAIRE_DOMAIN_SUBSTRINGS,
    extract_name_tokens,
    extract_nom_variants,
    is_annuaire_domain,
    is_generic_email,
    is_parent_group_site,
    normalize_for_email,
    validate_site_matches_company,
)
from advanced_enrichment import _generate_email_candidates


class TestValidateSiteMatchesCompany(unittest.TestCase):
    """Cas central de la spec : BERTHIAND AUTOMOBILES → renault.fr rejeté.

    'automobiles' est dans la stop-list, seul 'berthiand' est discriminant.
    Le slug du domaine 'renault' ne contient pas 'berthiand', et le fetch
    de renault.fr ne contient pas 'berthiand' non plus → rejet.
    """

    def test_berthiand_renault_rejected_lexical(self):
        # Étape 1 (match lexical) : doit suffire à rejeter renault.fr
        # car 'berthiand' n'est pas dans 'renault'.
        with patch("validators.requests.get") as mock_get:
            # Simuler un fetch qui ne contient pas 'berthiand'
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.iter_content = lambda chunk_size, decode_unicode: iter([
                "<html><head><title>Renault — Constructeur</title></head>"
            ])
            mock_get.return_value = mock_resp
            self.assertFalse(
                validate_site_matches_company("https://www.renault.fr", "BERTHIAND AUTOMOBILES")
            )

    def test_berthiand_own_site_accepted(self):
        # Si le site est berthiand-automobiles.fr → accepté au niveau lexical
        self.assertTrue(
            validate_site_matches_company("https://www.berthiand-automobiles.fr", "BERTHIAND AUTOMOBILES")
        )

    def test_annuaire_pagesjaunes_rejected(self):
        # Un annuaire ne doit JAMAIS être validé comme site officiel
        self.assertFalse(
            validate_site_matches_company("https://www.pagesjaunes.fr/martin-dupont", "MARTIN DUPONT SARL")
        )
        self.assertTrue(is_annuaire_domain("https://www.pagesjaunes.fr/martin-dupont"))

    def test_annuaire_societe_com_rejected(self):
        self.assertTrue(is_annuaire_domain("https://www.societe.com/societe/foo"))
        self.assertFalse(
            validate_site_matches_company("https://www.societe.com/societe/foo", "FOO SARL")
        )


class TestExtractNomVariants(unittest.TestCase):
    """Patterns email gèrent : noms composés, particules, accents."""

    def test_de_la_fontaine(self):
        # "de la Fontaine" : particules + nom de famille → variantes attendues
        variants = extract_nom_variants("de la Fontaine")
        self.assertIn("fontaine", variants)
        self.assertIn("delafontaine", variants)
        self.assertIn("lafontaine", variants)
        # Le dernier mot significatif doit venir en premier
        self.assertEqual(variants[0], "fontaine")

    def test_nom_compose_avec_tiret(self):
        # "MARTIN-DUPONT" : composé → variantes incl. dupont, martin-dupont
        variants = extract_nom_variants("MARTIN-DUPONT")
        self.assertIn("dupont", variants)
        self.assertIn("martin-dupont", variants)
        self.assertIn("martindupont", variants)
        self.assertIn("martin", variants)
        # Plus probable en premier
        self.assertEqual(variants[0], "dupont")

    def test_apostrophe_d_artagnan(self):
        # "D'Artagnan" : apostrophe → "artagnan" + "dartagnan"
        variants = extract_nom_variants("D'Artagnan")
        self.assertIn("artagnan", variants)

    def test_accents(self):
        # Les accents doivent être strippés pour l'email
        variants = extract_nom_variants("DUPRÉ")
        self.assertIn("dupre", variants)
        self.assertNotIn("dupré", variants)

    def test_normalize_for_email_jean_marc(self):
        self.assertEqual(normalize_for_email("Jean-Marc"), "jean-marc")
        self.assertEqual(normalize_for_email("François"), "francois")


class TestEmailCandidates(unittest.TestCase):
    """Le générateur de candidats produit les bons patterns sur un dirigeant
    avec prénom composé + particule."""

    def test_jean_marc_de_la_fontaine(self):
        prenom_usuel = normalize_for_email("Jean")  # premier prénom
        nom_variants = extract_nom_variants("de la Fontaine")
        candidates = _generate_email_candidates(prenom_usuel, nom_variants, "fontaine.fr")

        # Patterns attendus en haut de liste
        self.assertIn("jean.fontaine@fontaine.fr", candidates)
        self.assertIn("j.fontaine@fontaine.fr", candidates)
        # Variante avec particule collée
        self.assertTrue(
            any(c.startswith("jean.delafontaine@") or c.startswith("jean.lafontaine@") for c in candidates)
        )

    def test_no_duplicates(self):
        candidates = _generate_email_candidates("dupont", ["dupont"], "example.com")
        self.assertEqual(len(candidates), len(set(candidates)))


class TestIsParentGroupSite(unittest.TestCase):
    """Détection groupe parent (ex: renault.fr pour BERTHIAND AUTOMOBILES)."""

    def test_renault_pour_berthiand(self):
        # 'BERTHIAND AUTOMOBILES' → tokens=['berthiand'] (1 token, pas 2)
        # → la fonction est concue pour >= 2 tokens donc retourne False ici.
        # On teste plutôt un cas avec 2 tokens significatifs.
        self.assertFalse(is_parent_group_site("https://renault.fr", "BERTHIAND AUTOMOBILES"))

    def test_haribo_pour_haribo_uzes(self):
        # "HARIBO RICQLES ZAN" → tokens=['haribo', 'ricqles', 'zan']
        # haribo.com matche 'haribo' mais pas 'ricqles' ni 'zan' → groupe parent
        self.assertTrue(is_parent_group_site("https://haribo.com", "HARIBO RICQLES ZAN"))


class TestExtractNameTokens(unittest.TestCase):
    def test_stop_words_filtered(self):
        # 'AUTOMOBILES' et 'DE' sont stop-words → ne restent que les tokens propres
        tokens = extract_name_tokens("BERTHIAND AUTOMOBILES DE LYON")
        self.assertIn("berthiand", tokens)
        self.assertIn("lyon", tokens)
        self.assertNotIn("automobiles", tokens)
        self.assertNotIn("de", tokens)

    def test_acronyme_court(self):
        # Seuil 3 chars pour récupérer A68, MSF, IBM
        tokens = extract_name_tokens("AMBULANCES A68")
        self.assertIn("ambulances", tokens)
        self.assertIn("a68", tokens)


class TestIsGenericEmail(unittest.TestCase):
    def test_contact_at(self):
        self.assertTrue(is_generic_email("contact@example.com"))
        self.assertTrue(is_generic_email("Direction@Example.com"))
        self.assertTrue(is_generic_email("rh@boite.fr"))

    def test_personnel_email(self):
        self.assertFalse(is_generic_email("jean.dupont@example.com"))
        self.assertFalse(is_generic_email("jdupont@example.com"))


if __name__ == "__main__":
    unittest.main()
