import copy
from decimal import Decimal

import httpx
from django.test import TestCase

from . import cardmarket
from .models import Card, CardmarketPrice
from .tests import LedgerTestCase, cm_id, load_catalog, game

GUIDE = {
    "version": 1, "createdAt": "2026-11-07T02:45:31+0100",
    "priceGuides": [
        {"idProduct": 555001, "idCategory": 1, "avg": 40.5, "low": 35.0, "trend": 42.0, "avg1": 41.0,
         "avg7": 41.5, "avg30": 43.0, "avg-foil": 60.0, "low-foil": None, "trend-foil": 61.25,
         "avg1-foil": 59.0, "avg7-foil": 60.5, "avg30-foil": 62.0},
        {"idProduct": 555201, "idCategory": 1, "trend": 1.2},
        {"idProduct": 999999, "idCategory": 1, "trend": 99},
    ],
}

HOLO_GUIDE = {
    "version": 1, "createdAt": "2026-11-07T02:45:31+0100",
    "priceGuides": [
        {"idProduct": 700001, "idCategory": 51, "avg": 2.0, "low": 1.0, "trend": 2.5, "avg1": 2.1, "avg7": 2.2,
         "avg30": 2.3, "avg-holo": 8.0, "low-holo": 6.0, "trend-holo": 9.5, "avg1-holo": 8.1, "avg7-holo": 8.2,
         "avg30-holo": 8.3},
    ],
}


class FakeGuide:
    """Cardmarket's price guide files, one per game, served through httpx.MockTransport."""

    def __init__(self, guides=None):
        self.guides = copy.deepcopy(guides if guides is not None else {1: GUIDE})
        self.fail = False
        self.raw = None
        self.urls = []

    def handler(self, request):
        url = str(request.url)
        self.urls.append(url)
        for cm_game_id, guide in self.guides.items():
            if url == cardmarket.price_guide_url(cm_game_id):
                if self.fail:
                    return httpx.Response(500, text="boom")
                if self.raw is not None:
                    return httpx.Response(200, content=self.raw)
                return httpx.Response(200, json=guide)
        return httpx.Response(404)

    def client(self):
        return cardmarket.make_client(transport=httpx.MockTransport(self.handler))


class FetchGuideTest(TestCase):
    def test_guide_url_is_per_game(self):
        self.assertEqual(cardmarket.price_guide_url(6),
                         "https://downloads.s3.cardmarket.com/productCatalog/priceGuide/price_guide_6.json")

    def test_foil_prices_in_price_fields_order(self):
        fake = FakeGuide()
        prices, guide_date = cardmarket.fetch_guide(fake.client(), 1)
        self.assertEqual(fake.urls, [cardmarket.price_guide_url(1)])
        self.assertEqual(guide_date.isoformat(), "2026-11-07T02:45:31+01:00")
        self.assertEqual(prices[555001], (35.0, 42.0, 40.5, 41.0, 41.5, 43.0, None, 61.25, 60.0, 59.0, 60.5, 62.0))
        self.assertEqual(prices[555201], (None, 1.2, None, None, None, None, None, None, None, None, None, None))
        self.assertIn(999999, prices)  # products not in the catalog are kept too

    def test_holo_prices_fill_the_foil_fields(self):
        prices, _ = cardmarket.fetch_guide(FakeGuide({6: HOLO_GUIDE}).client(), 6)
        self.assertEqual(prices[700001], (1.0, 2.5, 2.0, 2.1, 2.2, 2.3, 6.0, 9.5, 8.0, 8.1, 8.2, 8.3))

    def test_http_error_raises(self):
        fake = FakeGuide()
        fake.fail = True
        with self.assertRaises(cardmarket.CardmarketError):
            cardmarket.fetch_guide(fake.client(), 1)

    def test_malformed_guide_raises(self):
        fake = FakeGuide()
        fake.raw = b"{not json"
        with self.assertRaises(cardmarket.CardmarketError):
            cardmarket.fetch_guide(fake.client(), 1)

    def test_unknown_game_raises(self):
        with self.assertRaises(cardmarket.CardmarketError):
            cardmarket.fetch_guide(FakeGuide().client(), 99)


class SuggestedPriceTest(LedgerTestCase):
    def setUp(self):
        super().setUp()
        load_catalog()
        CardmarketPrice.objects.create(id_product=cm_id(1001), trend=Decimal("42.00"), trend_foil=Decimal("61.25"))
        CardmarketPrice.objects.create(id_product=cm_id(1201), trend=Decimal("1.20"), trend_foil=Decimal("3.00"))

    def test_non_foil_uses_trend_via_product_link(self):
        card = Card.objects.create(game=game(), name="Ragavan", set_code="MH2", collector_number="138", cm_product_id=cm_id(1001))
        self.assertEqual(cardmarket.suggested_price(card, foil=False), Decimal("42.00"))

    def test_foil_uses_trend_foil(self):
        card = Card.objects.create(game=game(), name="Ragavan", set_code="MH2", collector_number="138", cm_product_id=cm_id(1001))
        self.assertEqual(cardmarket.suggested_price(card, foil=True), Decimal("61.25"))

    def test_foil_without_foil_trend(self):
        CardmarketPrice.objects.filter(pk=cm_id(1201)).update(trend_foil=None)
        card = Card.objects.create(game=game(), name="Counterspell", set_code="DMR", collector_number="45",
                                   cm_product_id=cm_id(1201))
        self.assertIsNone(cardmarket.suggested_price(card, foil=True))

    def test_zero_trend_is_no_price(self):
        CardmarketPrice.objects.filter(pk=cm_id(1201)).update(trend_foil=Decimal("0.00"))
        card = Card.objects.create(game=game(), name="Counterspell", set_code="DMR", collector_number="45",
                                   cm_product_id=cm_id(1201))
        self.assertIsNone(cardmarket.suggested_price(card, foil=True))

    def test_card_without_product_found_by_set_and_number(self):
        card = Card.objects.create(game=game(), name="Counterspell", set_code="DMR", collector_number="045")
        self.assertEqual(cardmarket.suggested_price(card, foil=False), Decimal("1.20"))

    def test_unknown_printing(self):
        card = Card.objects.create(game=game(), name="X", set_code="PLST", collector_number="9")
        self.assertIsNone(cardmarket.suggested_price(card, foil=False))

    def test_product_without_price_row(self):
        card = Card.objects.create(game=game(), name="Solitude", set_code="MH2", collector_number="32", cm_product_id=cm_id(1002))
        self.assertIsNone(cardmarket.suggested_price(card, foil=False))
