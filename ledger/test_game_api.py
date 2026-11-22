"""The collection, CSV import and export scoped to a game with ?game."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from . import services
from .models import Blueprint, Card, Category, CollectionItem, Expansion, Game, Listing, OrderItem, SyncLog
from .tests import LedgerTestCase, game, load_catalog, order_payload


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


class SaleGameTest(LedgerTestCase):
    def setUp(self):
        super().setUp()
        load_catalog()
        self.expansion, self.blueprint, self.item = pokemon_setup(self)
        self.listing = Listing.objects.create(item=self.item, price="1.00", quantity=1,
                                              status=Listing.Status.TO_PUBLISH)

    def test_listings_per_game(self):
        resp = self.client.get(reverse("ledger:api_listings"), {"game": "pokemon"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual([r["name"] for r in data["results"]], ["Pikachu"])
        self.assertEqual(data["stats"]["to_publish"], 1)
        data = self.client.get(reverse("ledger:api_listings")).json()
        self.assertNotIn("Pikachu", [r["name"] for r in data["results"]])

    def test_publish_only_the_game(self):
        self.assertTrue(Listing.objects.owned_by(self.user).filter(status=Listing.Status.TO_PUBLISH)
                        .exclude(pk=self.listing.pk).exists())
        resp = self.client.post(reverse("ledger:api_publish") + "?game=mtg")
        self.assertEqual(resp.status_code, 200)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, Listing.Status.TO_PUBLISH)

    def test_listing_detail_has_game(self):
        resp = self.client.get(reverse("ledger:api_listing", args=[self.item.pk]))
        self.assertEqual(resp.json()["game"], "pokemon")


class PublishGameTest(LedgerTestCase):
    """Publishing listings and importing orders of a game other than Magic on CardTrader."""

    def setUp(self):
        super().setUp()
        load_catalog()
        self.expansion, self.blueprint, self.item = pokemon_setup(self)
        self.item.foil = True
        self.item.save(update_fields=["foil"])
        self.listing = Listing.objects.create(item=self.item, price="3.00", quantity=1,
                                              status=Listing.Status.TO_PUBLISH)

    def publish_game(self, slug):
        resp = self.client.post(reverse("ledger:api_publish") + f"?game={slug}")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def test_publish_pokemon(self):
        self.publish_game("pokemon")
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, Listing.Status.PUBLISHED)
        product = self.ct.products[self.listing.ct_product_id]
        self.assertEqual(product["blueprint_id"], 2001)
        self.assertEqual(product["properties_hash"],
                         {"condition": "Near Mint", "pokemon_language": "en", "pokemon_reverse": True})

    def test_ct_game_id_is_looked_up_once(self):
        Game.objects.filter(slug="pokemon").update(ct_game_id=None)
        self.publish_game("pokemon")
        self.assertIn(("GET", "/games"), self.ct.calls)
        self.assertEqual(Game.by_slug("pokemon").ct_game_id, 5)
        Listing.objects.filter(pk=self.listing.pk).update(status=Listing.Status.TO_PUBLISH)
        self.ct.calls.clear()
        self.publish_game("pokemon")
        self.assertNotIn(("GET", "/games"), self.ct.calls)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, Listing.Status.PUBLISHED)

    def test_game_without_ct_game_id_fails_cleanly(self):
        card = Card.objects.create(game=game("lorcana"), name="Mickey Mouse", set_code="TFC", collector_number="1")
        item = CollectionItem.objects.create(owner=self.user, card=card, condition="NM", language="EN", quantity=1)
        listing = Listing.objects.create(item=item, price="2.00", quantity=1, status=Listing.Status.TO_PUBLISH)
        data = self.publish_game("lorcana")
        self.assertIn("warning", [m["level"] for m in data["messages"]])
        self.assertIn("game not sold on CardTrader", " ".join(m["text"] for m in data["messages"]))
        listing.refresh_from_db()
        self.assertEqual(listing.status, Listing.Status.TO_PUBLISH)
        self.assertIsNone(game("lorcana").ct_game_id)
        self.assertTrue(SyncLog.objects.filter(owner=self.user, message__contains="Mickey Mouse not published").exists())

    def test_same_code_other_game_is_ignored(self):
        # A Magic expansion with the Pokémon set's code, holding a card with the same Cardmarket id and number.
        self.ct.expansions.append({"id": 98, "game_id": 1, "code": "sv1", "name": "Scarlet & Violet"})
        self.ct.blueprints[98] = [{"id": 9801, "category_id": 1, "editable_properties": [], "name": "Pikachu",
                                   "fixed_properties": {"collector_number": "025"}, "card_market_ids": [700001]}]
        self.publish_game("pokemon")
        self.listing.refresh_from_db()
        self.assertEqual(self.ct.products[self.listing.ct_product_id]["blueprint_id"], 2001)
        self.assertNotIn(("/blueprints/export", {"expansion_id": "98"}), self.ct.call_params)

    def test_order_line_of_other_game(self):
        self.publish_game("pokemon")
        self.listing.refresh_from_db()
        order = order_payload(9001, "paid", [(self.listing.ct_product_id, "Pikachu", 1)])
        order["order_items"][0]["properties"] = {"condition": "Near Mint", "pokemon_language": "it",
                                                 "pokemon_reverse": True}
        self.ct.orders = [order]
        services.import_orders(self.ct.client(), services.get_cardtrader(self.user))
        line = OrderItem.objects.get(ct_id=90010)
        self.assertEqual(line.language, "IT")
        self.assertIs(line.foil, True)
        self.assertEqual(line.listing, self.listing)
