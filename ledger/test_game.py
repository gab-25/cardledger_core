"""The games of the catalog: each expansion, category and card belongs to one."""

from django.db import IntegrityError, transaction
from django.test import TestCase

from .models import MAGIC, Blueprint, Card, Category, Expansion, Game
from .tests import game


class GameTest(TestCase):
    def test_six_games_exist(self):
        self.assertEqual(
            list(Game.objects.order_by("cm_id").values_list("slug", "name", "cm_id", "ct_game_id")),
            [
                ("mtg", "Magic: The Gathering", 1, 1),
                ("yugioh", "Yu-Gi-Oh!", 3, None),
                ("pokemon", "Pokémon", 6, None),
                ("onepiece", "One Piece", 18, None),
                ("lorcana", "Disney Lorcana", 19, None),
                ("riftbound", "Riftbound", 22, None),
            ],
        )
        self.assertEqual(Game.by_slug(MAGIC).name, "Magic: The Gathering")
        self.assertEqual(str(Game.by_slug("pokemon")), "Pokémon")
        with self.assertRaises(Game.DoesNotExist):
            Game.by_slug("chess")

    def test_same_expansion_code_in_two_games(self):
        Expansion.objects.create(game=game("pokemon"), code="sv1", name="Scarlet & Violet")
        Expansion.objects.create(game=game("lorcana"), code="sv1", name="Another sv1")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Expansion.objects.create(game=game("pokemon"), code="sv1", name="Twin")
        Category.objects.create(game=game("pokemon"), cm_id=1, name="Pokémon Single")
        Category.objects.create(game=game(MAGIC), cm_id=1, name="Magic Single")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Category.objects.create(game=game(MAGIC), cm_id=1, name="Twin")

    def test_same_card_printing_in_two_games(self):
        Card.objects.create(game=game(MAGIC), name="Pikachu", set_code="SV1", collector_number="25")
        Card.objects.create(game=game("pokemon"), name="Pikachu", set_code="SV1", collector_number="25")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Card.objects.create(game=game("pokemon"), name="Raichu", set_code="SV1", collector_number="25")
        Card.objects.create(game=game(MAGIC), name="Loose", set_code="ZZZ")
        Card.objects.create(game=game("pokemon"), name="Loose", set_code="ZZZ")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Card.objects.create(game=game("pokemon"), name="Loose", set_code="ZZZ")

    def test_blueprint_is_product_follows_category_is_single(self):
        pokemon = game("pokemon")
        expansion = Expansion.objects.create(game=pokemon, code="sv1", name="Scarlet & Violet")
        single = Category.objects.create(game=pokemon, cm_id=51, name="Pokémon Single", is_single=True)
        sealed = Category.objects.create(game=pokemon, cm_id=1, name="Pokémon Booster")
        self.assertFalse(Blueprint(expansion=expansion, name="Pikachu", category=single).is_product)
        self.assertTrue(Blueprint(expansion=expansion, name="Booster", category=sealed).is_product)
        self.assertFalse(Blueprint(expansion=expansion, name="Unknown").is_product)

    def test_set_codes_up_to_40_characters(self):
        code = "x" * 40
        expansion = Expansion.objects.create(game=game(), code=code, name="Long")
        card = Card.objects.create(game=game(), name="Card", set_code=code.upper(), collector_number="1")
        expansion.refresh_from_db()
        card.refresh_from_db()
        self.assertEqual((expansion.code, card.set_code), (code, code.upper()))
