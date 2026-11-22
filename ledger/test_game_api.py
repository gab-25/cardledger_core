"""The collection, CSV import and export scoped to a game with ?game."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from . import services
from .models import Blueprint, Card, Category, CollectionItem, Expansion, Game, Listing, OrderItem, SyncLog
from .tests import POKEMON_PROPS, LedgerTestCase, game, load_catalog, order_payload


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
        resp = self.client.get(url, {"game": "pokemon"})
        self.assertIn("cardledger-pokemon-", resp["Content-Disposition"])
        self.assertTrue(SyncLog.objects.filter(message__startswith="CSV export: Pokémon collection exported").exists())
        magic = self.client.get(url).content.decode()
        self.assertIn("cardledger-mtg-", self.client.get(url)["Content-Disposition"])
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
        magic = Listing.objects.owned_by(self.user).filter(status=Listing.Status.TO_PUBLISH).exclude(pk=self.listing.pk)
        magic_ids = list(magic.values_list("pk", flat=True))
        resp = self.client.post(reverse("ledger:api_publish") + "?game=mtg")
        self.assertEqual(resp.status_code, 200)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, Listing.Status.TO_PUBLISH)
        self.assertFalse(Listing.objects.filter(pk__in=magic_ids, status=Listing.Status.TO_PUBLISH).exists())

    def test_publish_without_game_is_magic(self):
        magic_ids = list(Listing.objects.owned_by(self.user).filter(status=Listing.Status.TO_PUBLISH)
                         .exclude(pk=self.listing.pk).values_list("pk", flat=True))
        self.assertTrue(magic_ids)
        resp = self.client.post(reverse("ledger:api_publish"))
        self.assertEqual(resp.status_code, 200)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, Listing.Status.TO_PUBLISH)
        self.assertFalse(Listing.objects.filter(pk__in=magic_ids, status=Listing.Status.TO_PUBLISH).exists())

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
        self.assertEqual(self.ct.products[self.listing.ct_product_id]["properties_hash"],
                         {"condition": "Near Mint", "pokemon_language": "en", "pokemon_reverse": True})

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


class UndescribedListingTest(LedgerTestCase):
    """A Pokémon listing whose language or foil CardTrader can't be told is never published; the others are."""

    UNKNOWN = "CardTrader properties unknown"
    NO_FOIL = "foil not offered on CardTrader for this blueprint"

    def setUp(self):
        super().setUp()
        load_catalog()
        self.expansion, self.blueprint, self.item = pokemon_setup(self)
        self.listing = Listing.objects.create(item=self.item, price="3.00", quantity=1,
                                              status=Listing.Status.TO_PUBLISH)
        # A second Pokémon card, always describable: it must publish whatever happens to Pikachu.
        self.ct.blueprints[20].append({"id": 2002, "category_id": 73, "editable_properties": POKEMON_PROPS,
                                       "name": "Charmander", "fixed_properties": {"collector_number": "004"},
                                       "card_market_ids": [700004]})
        card = Card.objects.create(game=game("pokemon"), name="Charmander", set_code="SV1", collector_number="004",
                                   cm_product_id=700004)
        item = CollectionItem.objects.create(owner=self.user, card=card, condition="NM", language="EN", quantity=1)
        self.other = Listing.objects.create(item=item, price="1.00", quantity=1, status=Listing.Status.TO_PUBLISH)

    def pikachu(self):
        return self.ct.blueprints[20][0]

    def assert_fails(self, reason):
        resp = self.client.post(reverse("ledger:api_publish") + "?game=pokemon")
        self.assertEqual(resp.status_code, 200, resp.content)
        texts = [m["text"] for m in resp.json()["messages"] if m["level"] == "warning"]
        self.assertEqual(texts, [f"Pikachu: {reason}"])
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, Listing.Status.TO_PUBLISH)
        self.assertNotIn(2001, [p["blueprint_id"] for p in self.ct.products.values()])
        self.other.refresh_from_db()
        self.assertEqual(self.other.status, Listing.Status.PUBLISHED)
        self.assertTrue(SyncLog.objects.filter(owner=self.user, message__contains=f"Pikachu not published ({reason})")
                        .exists())

    def test_blueprint_without_editable_properties(self):
        del self.pikachu()["editable_properties"]
        self.assert_fails(self.UNKNOWN)

    def test_linked_blueprint_missing_from_export(self):
        Card.objects.filter(name="Pikachu").update(ct_blueprint_id=2050)
        self.assert_fails(self.UNKNOWN)

    def test_linked_card_whose_expansion_is_gone(self):
        Card.objects.filter(name="Pikachu").update(ct_blueprint_id=2001, set_code="OLD")
        self.ct.expansions = [e for e in self.ct.expansions if e["code"] != "old"]
        Expansion.objects.filter(pk=self.expansion.pk).update(name="Renamed")
        self.assert_fails(self.UNKNOWN)

    def test_no_language_property(self):
        self.pikachu()["editable_properties"] = [p for p in POKEMON_PROPS if p["name"] != "pokemon_language"]
        self.assert_fails(self.UNKNOWN)

    def test_foil_not_offered(self):
        CollectionItem.objects.filter(pk=self.item.pk).update(foil=True)
        self.pikachu()["editable_properties"] = [p for p in POKEMON_PROPS if p["name"] != "pokemon_reverse"]
        self.assert_fails(self.NO_FOIL)


class MagicPayloadTest(LedgerTestCase):
    """A Magic listing's CardTrader payload is what it always was, whatever CardTrader says the blueprint takes."""

    def payload(self, accepted, *, foil=False, product=False):
        n = Card.objects.count()
        card = Card.objects.create(game=game(), name=f"Thing {n}", set_code="XYZ", collector_number=str(n),
                                   ct_blueprint_id=4242 + n, is_product=product)
        item = CollectionItem.objects.create(owner=self.user, card=card, condition="NM", language="IT", foil=foil,
                                             quantity=1)
        listing = Listing.objects.create(item=item, price="2.00", quantity=1)
        return services.product_payload(listing, accepted)["properties"]

    def test_single_always_sends_language_and_foil(self):
        expected = {"condition": "Near Mint", "mtg_language": "it", "mtg_foil": True}
        self.assertEqual(self.payload(["condition"], foil=True), expected)
        self.assertEqual(self.payload(None, foil=True), expected)
        self.assertEqual(self.payload([], foil=False), {"condition": "Near Mint", "mtg_language": "it"})

    def test_product(self):
        self.assertEqual(self.payload(None, product=True), {"mtg_language": "it"})
        self.assertEqual(self.payload([], product=True), {})
        self.assertEqual(self.payload(["condition", "mtg_language"], product=True),
                         {"condition": "Near Mint", "mtg_language": "it"})
