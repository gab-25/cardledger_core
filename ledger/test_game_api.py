"""The collection, CSV import and export scoped to a game with ?game."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from . import services
from .models import Blueprint, Card, Category, CollectionItem, Expansion
from .tests import LedgerTestCase, game, load_catalog


def pokemon_setup(test):
    """A Pokémon expansion, a singles category, a blueprint and test.user's two copies of it, as a Card of its game.
    Returns (expansion, blueprint, item)."""
    pokemon = game("pokemon")
    expansion = Expansion.objects.create(game=pokemon, code="sv1", name="Scarlet & Violet")
    single = Category.objects.create(game=pokemon, cm_id=1, name="Pokémon Single", is_single=True)
    blueprint = Blueprint.objects.create(cm_product_id=700001, expansion=expansion, name="Pikachu",
                                         collector_number="025", category=single)
    card = Card.objects.create(game=pokemon, name="Pikachu", set_code="SV1", collector_number="025",
                               cm_product_id=700001)
    item = CollectionItem.objects.create(owner=test.user, card=card, condition="NM", language="EN", quantity=2)
    return expansion, blueprint, item


class CollectionGameTest(LedgerTestCase):
    def setUp(self):
        super().setUp()
        load_catalog()
        self.expansion, self.blueprint, self.item = pokemon_setup(self)

    def collection(self, **params):
        resp = self.client.get(reverse("ledger:api_collection"), params)
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def names(self, **params):
        return [r["name"] for r in self.collection(**params)["results"]]

    def test_default_is_magic(self):
        self.assertNotIn("Pikachu", self.names())
        self.assertEqual(self.collection()["stats"]["total"], self.collection(game="mtg")["stats"]["total"])

    def test_pokemon_only(self):
        data = self.collection(game="pokemon")
        self.assertEqual([r["name"] for r in data["results"]], ["Pikachu"])
        self.assertEqual(data["stats"]["total"], 2)
        self.assertEqual(data["sets"], ["SV1"])
        self.assertEqual(data["categories"], [{"id": 1, "name": self.blueprint.category.name}])

    def test_category_filter_is_per_game(self):
        self.assertEqual(self.names(game="pokemon", category="1"), ["Pikachu"])
        self.assertNotIn("Pikachu", self.names(game="mtg", category="1"))

    def test_item_detail_has_game(self):
        resp = self.client.get(reverse("ledger:api_item", args=[self.item.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["game"], "pokemon")

    def test_export_per_game(self):
        url = reverse("ledger:api_export")
        pokemon = self.client.get(url, {"game": "pokemon"}).content.decode()
        self.assertIn("Pikachu", pokemon)
        self.assertEqual(len(pokemon.strip().splitlines()), 2)
        magic = self.client.get(url).content.decode()
        self.assertNotIn("Pikachu", magic)
        self.assertGreater(len(magic.strip().splitlines()), 1)

    def test_import_into_game(self):
        header = ",".join(services.CSV_FIELDS)
        upload = SimpleUploadedFile("c.csv", f"{header}\nPikachu,SV1,NM,EN,0,1,025\n".encode(), "text/csv")
        resp = self.client.post(reverse("ledger:api_import") + "?game=pokemon", {"file": upload})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual({c.game.slug for c in Card.objects.filter(name="Pikachu", set_code="SV1")}, {"pokemon"})
        self.assertEqual(CollectionItem.objects.filter(owner=self.user, card__name="Pikachu").count(), 2)
