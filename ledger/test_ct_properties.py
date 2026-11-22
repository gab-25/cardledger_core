"""The CardTrader property names of each game: language and foil, when publishing and in order lines."""

from django.test import TestCase

from .ct_properties import foil_key, language_key, read_foil, read_language
from .models import Game

POKEMON_NAMES = ["condition", "pokemon_language", "pokemon_reverse"]


class CtPropertiesTest(TestCase):
    def setUp(self):
        self.magic = Game.by_slug("mtg")
        self.pokemon = Game.by_slug("pokemon")

    def test_language_key(self):
        self.assertEqual(language_key(POKEMON_NAMES, self.pokemon), "pokemon_language")
        self.assertEqual(language_key(["condition", "mtg_language", "mtg_foil"], self.magic), "mtg_language")
        self.assertIsNone(language_key(["condition"], self.pokemon))

    def test_language_key_unknown_properties(self):
        self.assertEqual(language_key(None, self.magic), "mtg_language")
        self.assertIsNone(language_key(None, self.pokemon))

    def test_foil_key(self):
        self.assertEqual(foil_key(POKEMON_NAMES, self.pokemon), "pokemon_reverse")
        self.assertEqual(foil_key(["condition", "mtg_language", "mtg_foil"], self.magic), "mtg_foil")
        self.assertEqual(foil_key(None, self.magic), "mtg_foil")
        self.assertIsNone(foil_key(None, self.pokemon))
        self.assertIsNone(foil_key(["condition", "pokemon_language"], self.pokemon))

    def test_read_language(self):
        self.assertEqual(read_language({"onepiece_language": "jp"}), "JP")
        self.assertEqual(read_language({"condition": "Near Mint", "mtg_language": "en"}), "EN")
        self.assertEqual(read_language({"condition": "Near Mint"}), "")

    def test_read_foil(self):
        self.assertIs(read_foil({"mtg_foil": True}), True)
        self.assertIs(read_foil({"pokemon_reverse": True}), True)
        self.assertIs(read_foil({"mtg_foil": False}), False)
        self.assertIs(read_foil({"condition": "Near Mint"}), False)
