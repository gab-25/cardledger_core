"""No lookup mixes games: a Pokémon expansion with Magic's code "mh2" and a printing with Ragavan's number never
answers for a Magic card (and the other way round)."""

from datetime import datetime, timezone

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from . import catalog, services
from .cardmarket import owned_quantities
from .models import MAGIC, Blueprint, Card, CardmarketPrice, Category, CollectionItem, Expansion
from .recognition import Recognition, match
from .search import search_blueprints
from .tests import LedgerTestCase, cm_id, game, load_catalog

RAGAVAN = cm_id(1001)  # Magic: mh2 #138
PIKACHU = 100  # Pokémon: mh2 #138, a lower product id than every Magic one (so "the first by id" would pick it)


class GameScopingTest(LedgerTestCase):
    def setUp(self):
        super().setUp()
        load_catalog()
        self.magic, self.pokemon = game(MAGIC), game("pokemon")
        expansion = Expansion.objects.create(game=self.pokemon, code="mh2", name="Pokémon mh2")
        single = Category.objects.create(game=self.pokemon, cm_id=1, name="Pokémon Single", is_single=True)
        self.pikachu = Blueprint.objects.create(cm_product_id=PIKACHU, expansion=expansion, name="Pikachu",
                                                collector_number="138", category=single)
        CardmarketPrice.objects.create(id_product=RAGAVAN, trend="50.00",
                                       prices_date=datetime(2026, 11, 1, tzinfo=timezone.utc))
        CardmarketPrice.objects.create(id_product=PIKACHU, trend="3.00",
                                       prices_date=datetime(2026, 11, 9, tzinfo=timezone.utc))

    def card(self, game, name, number="138", **fields):
        return Card.objects.create(game=game, name=name, set_code="MH2", collector_number=number, **fields)

    def own(self, card, quantity=1):
        return CollectionItem.objects.create(owner=self.user, card=card, condition="NM", language="EN",
                                             quantity=quantity)

    def catalog(self, **params):
        resp = self.client.get(reverse("ledger:api_catalog"), params)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def ids(self, **params):
        return [r["ct_id"] for r in self.catalog(**params)["results"]]

    def test_link_blueprint_ignores_other_games(self):
        ragavan, pikachu = self.card(self.magic, "Ragavan"), self.card(self.pokemon, "Pikachu")
        services.link_blueprint(ragavan)
        services.link_blueprint(pikachu)
        self.assertEqual((ragavan.cm_product_id, pikachu.cm_product_id), (RAGAVAN, PIKACHU))

    def test_blueprint_of_stays_in_the_card_game(self):
        self.assertEqual(catalog.blueprint_of(self.card(self.magic, "Ragavan")).cm_product_id, RAGAVAN)
        # a Magic product id on a Pokémon card is not its printing: its set code and number are
        pikachu = self.card(self.pokemon, "Pikachu", cm_product_id=RAGAVAN)
        self.assertEqual(catalog.blueprint_of(pikachu).cm_product_id, PIKACHU)

    def test_card_for_blueprint_creates_card_in_blueprint_game(self):
        self.card(self.magic, "Ragavan", cm_product_id=RAGAVAN)
        card = catalog.card_for_blueprint(self.pikachu)
        self.assertEqual((card.game, card.name, card.set_code, card.collector_number, card.cm_product_id),
                         (self.pokemon, "Pikachu", "MH2", "138", PIKACHU))
        self.assertEqual(catalog.card_for_blueprint(Blueprint.objects.get(cm_product_id=RAGAVAN)).game, self.magic)

    def test_reconcile_never_folds_across_games(self):
        pikachu = Card.objects.create(game=self.pokemon, name="Pikachu", set_code="MH2", cm_product_id=PIKACHU)
        ragavan = self.card(self.magic, "Ragavan")  # unlinked, on the number the Pokémon card is rewritten to
        twin = Card.objects.create(game=self.magic, name="Ragavan", set_code="MH2", collector_number="X1",
                                   cm_product_id=PIKACHU)  # a Magic card wrongly on the Pokémon product
        items = [self.own(pikachu), self.own(ragavan), self.own(twin)]

        result = catalog.reconcile_cards()

        self.assertEqual((result.merged, result.clashes), (0, 0))
        for card in (pikachu, ragavan, twin):
            card.refresh_from_db()
        self.assertEqual((pikachu.game, pikachu.collector_number, pikachu.cm_product_id),
                         (self.pokemon, "138", PIKACHU))
        self.assertEqual((ragavan.game, ragavan.collector_number, ragavan.cm_product_id), (self.magic, "138", RAGAVAN))
        self.assertEqual([i.card_id for i in CollectionItem.objects.filter(pk__in=[i.pk for i in items])
                          .order_by("pk")], [pikachu.pk, ragavan.pk, twin.pk])
        # each game alone: the Pokémon pass touches no Magic card
        self.assertEqual(catalog.reconcile_cards(self.pokemon).merged, 0)

    def test_owned_quantities_only_counts_the_game(self):
        self.own(self.card(self.magic, "Ragavan"), 2)  # unlinked: found by set code + number
        self.own(self.card(self.pokemon, "Pikachu"), 3)
        self.assertEqual(owned_quantities(self.user, self.magic), {RAGAVAN: 2})
        self.assertEqual(owned_quantities(self.user, self.pokemon), {PIKACHU: 3})

    def test_catalog_api_defaults_to_magic(self):
        data = self.catalog(set="mh2")
        self.assertIn(RAGAVAN, [r["ct_id"] for r in data["results"]])
        self.assertNotIn(PIKACHU, [r["ct_id"] for r in data["results"]])
        self.assertNotIn(PIKACHU, self.ids())
        self.assertEqual(data["prices_date"][:10], "2026-11-01")  # Magic's prices, not Pokémon's newer guide

    def test_catalog_api_filters_by_game(self):
        self.assertEqual(self.ids(game="pokemon", set="mh2"), [PIKACHU])
        self.assertEqual(self.ids(game="pokemon"), [PIKACHU])
        self.assertEqual(self.ids(game="pokemon", category="1"), [PIKACHU])
        self.assertNotIn(PIKACHU, self.ids(game="mtg", category="1"))
        self.assertEqual(self.catalog(game="pokemon")["prices_date"][:10], "2026-11-09")

    def test_catalog_api_unknown_game_is_400(self):
        for url in (reverse("ledger:api_catalog"), reverse("ledger:api_catalog_categories")):
            resp = self.client.get(url, {"game": "chess"})
            self.assertEqual(resp.status_code, 400)
            self.assertIn("game", resp.json())

    def test_categories_api_filters_by_game(self):
        url = reverse("ledger:api_catalog_categories")
        self.assertEqual(self.client.get(url, {"game": "pokemon"}).json(), [{"id": 1, "name": "Pokémon Single"}])
        self.assertNotIn("Pokémon Single", [c["name"] for c in self.client.get(url).json()])

    def test_search_does_not_return_other_games(self):
        self.assertEqual(self.ids(q="mh2 138"), [RAGAVAN])
        self.assertEqual(self.ids(q="pikachu"), [])
        self.assertEqual(self.ids(game="pokemon", q="mh2 138"), [PIKACHU])
        magic = Blueprint.objects.filter(expansion__game=self.magic)
        self.assertEqual([b.cm_product_id for b in search_blueprints(magic, "mh2 138")], [RAGAVAN])

    def test_recognition_searches_only_magic(self):
        m = match(Recognition(name="Pikachu", language="EN"), None)
        self.assertEqual((m.reason, m.candidates), ("not_found", []))
        m = match(Recognition(name="Pikachu", set_code="mh2", collector_number="138", language="EN"), None)
        self.assertNotIn(PIKACHU, [b.cm_product_id for b in m.candidates])
        self.assertEqual(catalog.expansion_by_code("mh2", self.pokemon), self.pikachu.expansion)
        self.assertEqual(catalog.expansion_by_code("MH2", self.magic).game, self.magic)

    def test_csv_import_links_only_magic(self):
        upload = SimpleUploadedFile("c.csv", b"name,set,condition,language,foil,quantity,number\n"
                                             b"Ragavan,MH2,NM,EN,0,1,138\n")
        resp = self.client.post(reverse("ledger:api_import"), {"file": upload})
        self.assertEqual(resp.status_code, 200)
        card = Card.objects.get(set_code="MH2", collector_number="138")
        self.assertEqual((card.game, card.cm_product_id), (self.magic, RAGAVAN))
