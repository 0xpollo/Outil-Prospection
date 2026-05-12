"""Tests unitaires de la refonte v2 (mai 2026).

Lancer : python3 -m unittest test_enrichment.py
"""

import unittest
from unittest.mock import patch, MagicMock

from validators import (
    extract_name_tokens,
    extract_nom_variants,
    is_annuaire_domain,
    is_generic_email,
    is_parent_group_site,
    normalize_for_email,
    validate_site_matches_company,
)
from email_processor import (
    PUBLIC_EMAIL_DOMAINS,
    assign_destinataire_ranks,
    classify_tier,
    is_franchise,
    is_public_email,
    pick_best_email,
)
from pattern_finder import generate_dirigeant_patterns
import mx_classifier


# ============================================================================
# Validation du site officiel (cœur historique du pipeline)
# ============================================================================

class TestValidateSiteMatchesCompany(unittest.TestCase):
    """BERTHIAND AUTOMOBILES → renault.fr rejeté (groupe parent)."""

    def test_berthiand_renault_rejected_lexical(self):
        with patch("validators.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.iter_content = lambda chunk_size, decode_unicode: iter([
                "<html><head><title>Renault</title></head>"
            ])
            mock_get.return_value = mock_resp
            self.assertFalse(
                validate_site_matches_company(
                    "https://www.renault.fr", "BERTHIAND AUTOMOBILES"
                )
            )

    def test_berthiand_own_site_accepted(self):
        self.assertTrue(
            validate_site_matches_company(
                "https://www.berthiand-automobiles.fr", "BERTHIAND AUTOMOBILES"
            )
        )

    def test_annuaire_pagesjaunes_rejected(self):
        self.assertFalse(
            validate_site_matches_company(
                "https://www.pagesjaunes.fr/martin-dupont", "MARTIN DUPONT SARL"
            )
        )
        self.assertTrue(is_annuaire_domain("https://www.pagesjaunes.fr/martin"))


class TestIsParentGroupSite(unittest.TestCase):
    def test_haribo_pour_haribo_uzes(self):
        self.assertTrue(
            is_parent_group_site("https://haribo.com", "HARIBO RICQLES ZAN")
        )


class TestExtractNameTokens(unittest.TestCase):
    def test_stop_words_filtered(self):
        tokens = extract_name_tokens("BERTHIAND AUTOMOBILES DE LYON")
        self.assertIn("berthiand", tokens)
        self.assertIn("lyon", tokens)
        self.assertNotIn("automobiles", tokens)


# ============================================================================
# extract_nom_variants (gardé pour patterns + détection nominatif)
# ============================================================================

class TestExtractNomVariants(unittest.TestCase):
    def test_de_la_fontaine(self):
        variants = extract_nom_variants("de la Fontaine")
        self.assertIn("fontaine", variants)
        self.assertIn("delafontaine", variants)

    def test_nom_compose_avec_tiret(self):
        variants = extract_nom_variants("MARTIN-DUPONT")
        self.assertIn("dupont", variants)
        self.assertIn("martin-dupont", variants)
        self.assertIn("martin", variants)

    def test_apostrophe(self):
        variants = extract_nom_variants("D'Artagnan")
        self.assertIn("artagnan", variants)

    def test_accents(self):
        variants = extract_nom_variants("DUPRÉ")
        self.assertIn("dupre", variants)


class TestIsGenericEmail(unittest.TestCase):
    def test_contact_at(self):
        self.assertTrue(is_generic_email("contact@example.com"))
        self.assertTrue(is_generic_email("rh@boite.fr"))

    def test_personnel_email(self):
        self.assertFalse(is_generic_email("jean.dupont@example.com"))


# ============================================================================
# Génération de patterns dirigeant (uniquement utilisé sur MX discriminatif)
# ============================================================================

class TestGenerateDirigeantPatterns(unittest.TestCase):
    def test_jean_dupont(self):
        patterns = generate_dirigeant_patterns("Jean", "Dupont", "boite.fr")
        self.assertIn("jean.dupont@boite.fr", patterns)
        self.assertIn("j.dupont@boite.fr", patterns)
        self.assertIn("jdupont@boite.fr", patterns)
        self.assertIn("jean@boite.fr", patterns)
        self.assertIn("dupont@boite.fr", patterns)

    def test_no_duplicates(self):
        patterns = generate_dirigeant_patterns("Jean", "Dupont", "boite.fr")
        self.assertEqual(len(patterns), len(set(patterns)))

    def test_no_domain_returns_empty(self):
        self.assertEqual(generate_dirigeant_patterns("Jean", "Dupont", ""), [])

    def test_no_prenom_returns_empty(self):
        self.assertEqual(generate_dirigeant_patterns("", "Dupont", "boite.fr"), [])


# ============================================================================
# email_processor : rang destinataire + pick_best + tier
# ============================================================================

class TestIsPublicEmail(unittest.TestCase):
    def test_gmail(self):
        self.assertTrue(is_public_email("foo@gmail.com"))
        self.assertTrue(is_public_email("foo@orange.fr"))
        self.assertTrue(is_public_email("foo@hotmail.fr"))

    def test_corporate(self):
        self.assertFalse(is_public_email("foo@boite.fr"))
        self.assertFalse(is_public_email("contact@plombier-dupont.fr"))


class TestAssignDestinataireRanks(unittest.TestCase):
    def test_dirigeant_nominatif_gagne(self):
        ent = {
            "dirigeant_prenom": "Jean",
            "dirigeant_nom": "Dupont",
            "emails": [
                {"email": "contact@boite.fr", "is_public_domain": False},
                {"email": "jean.dupont@boite.fr", "is_public_domain": False},
                {"email": "rh@boite.fr", "is_public_domain": False},
            ],
        }
        assign_destinataire_ranks(ent)
        ranks = {e["email"]: e["destinataire_rank"] for e in ent["emails"]}
        self.assertEqual(ranks["jean.dupont@boite.fr"], 1)
        self.assertEqual(ranks["contact@boite.fr"], 4)
        self.assertEqual(ranks["rh@boite.fr"], 7)

    def test_perso_solo_rank_3(self):
        ent = {
            "emails": [
                {"email": "plombier@gmail.com", "is_public_domain": True},
            ],
        }
        assign_destinataire_ranks(ent)
        self.assertEqual(ent["emails"][0]["destinataire_rank"], 3)

    def test_perso_avec_corporate_rank_5(self):
        ent = {
            "emails": [
                {"email": "contact@boite.fr", "is_public_domain": False},
                {"email": "perso@gmail.com", "is_public_domain": True},
            ],
        }
        assign_destinataire_ranks(ent)
        for e in ent["emails"]:
            if e["email"] == "perso@gmail.com":
                self.assertEqual(e["destinataire_rank"], 5)


class TestPickBestEmail(unittest.TestCase):
    def test_valid_bat_catchall_meme_si_rang_pire(self):
        ent = {
            "emails": [
                {"email": "rh@boite.fr", "smtp_status": "valid",
                 "is_public_domain": False, "destinataire_rank": 7},
                {"email": "direction@boite.fr", "smtp_status": "catchall",
                 "is_public_domain": False, "destinataire_rank": 2},
            ],
        }
        best = pick_best_email(ent)
        self.assertEqual(best["email"], "rh@boite.fr")

    def test_rang_decide_a_validite_egale(self):
        ent = {
            "emails": [
                {"email": "contact@boite.fr", "smtp_status": "valid",
                 "is_public_domain": False, "destinataire_rank": 4},
                {"email": "jean.dupont@boite.fr", "smtp_status": "valid",
                 "is_public_domain": False, "destinataire_rank": 1},
            ],
        }
        best = pick_best_email(ent)
        self.assertEqual(best["email"], "jean.dupont@boite.fr")

    def test_aucun_email(self):
        self.assertIsNone(pick_best_email({"emails": []}))


class TestClassifyTier(unittest.TestCase):
    def test_p0_smtp_valid(self):
        ent = {
            "nom": "Foo",
            "emails": [
                {"email": "contact@boite.fr", "smtp_status": "valid",
                 "source": "published", "is_public_domain": False,
                 "destinataire_rank": 4},
            ],
        }
        self.assertEqual(classify_tier(ent), "P0")

    def test_p0_published_catchall(self):
        ent = {
            "nom": "Foo",
            "emails": [
                {"email": "contact@boite.fr", "smtp_status": "catchall",
                 "source": "published", "is_public_domain": False,
                 "destinataire_rank": 4},
            ],
        }
        self.assertEqual(classify_tier(ent), "P0")

    def test_p1_published_unknown(self):
        ent = {
            "nom": "Foo",
            "emails": [
                {"email": "contact@boite.fr", "smtp_status": "unknown",
                 "source": "published", "is_public_domain": False,
                 "destinataire_rank": 4},
            ],
        }
        self.assertEqual(classify_tier(ent), "P1")

    def test_p1_perso_solo_artisan(self):
        ent = {
            "nom": "Foo",
            "emails": [
                {"email": "plombier@gmail.com", "smtp_status": "",
                 "source": "published", "is_public_domain": True,
                 "destinataire_rank": 3},
            ],
        }
        self.assertEqual(classify_tier(ent), "P1")

    def test_p2_aucun_email(self):
        self.assertEqual(classify_tier({"nom": "Foo", "emails": []}), "P2")

    def test_x_franchise(self):
        self.assertEqual(
            classify_tier({"nom": "McDonald's Lyon", "emails": []}), "X"
        )

    def test_x_franchise_meme_avec_email(self):
        ent = {
            "nom": "Decathlon Lyon Confluence",
            "emails": [
                {"email": "contact@decathlon.fr", "smtp_status": "valid",
                 "source": "published", "is_public_domain": False,
                 "destinataire_rank": 4},
            ],
        }
        self.assertEqual(classify_tier(ent), "X")


class TestIsFranchise(unittest.TestCase):
    def test_chaines(self):
        self.assertTrue(is_franchise("Restaurant McDonald's Part-Dieu"))
        self.assertTrue(is_franchise("Carrefour Express Lyon"))
        self.assertTrue(is_franchise("Norauto Bordeaux"))
        self.assertTrue(is_franchise("Optical Center Marseille"))

    def test_pme_locale(self):
        self.assertFalse(is_franchise("Garage Dupont"))
        self.assertFalse(is_franchise("Plomberie Martin & Fils"))


# ============================================================================
# MX classifier (matchers — pas le réseau)
# ============================================================================

class TestMxClassifierPatterns(unittest.TestCase):
    def test_match_m365(self):
        provider, mx_type = mx_classifier._match_provider(
            ["foo-com.mail.protection.outlook.com"]
        )
        self.assertEqual(provider, "m365")
        self.assertEqual(mx_type, "discriminatif")

    def test_match_gworkspace(self):
        provider, mx_type = mx_classifier._match_provider(
            ["aspmx.l.google.com"]
        )
        self.assertEqual(provider, "gworkspace")
        self.assertEqual(mx_type, "discriminatif")

    def test_match_ovh(self):
        provider, mx_type = mx_classifier._match_provider(
            ["mx1.mail.ovh.net"]
        )
        self.assertEqual(provider, "ovh")
        self.assertEqual(mx_type, "opaque")

    def test_match_ionos(self):
        provider, mx_type = mx_classifier._match_provider(
            ["mx00.ionos.fr"]
        )
        self.assertEqual(provider, "ionos")
        self.assertEqual(mx_type, "opaque")

    def test_match_inconnu(self):
        provider, mx_type = mx_classifier._match_provider(
            ["smtp-in.exoticprovider.net"]
        )
        self.assertIsNone(provider)


if __name__ == "__main__":
    unittest.main()
