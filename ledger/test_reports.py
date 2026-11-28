"""The reports API: KPIs with the previous period, game and market filters, weeks, markets, sets, countries, CSV."""

import csv
import io
from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from .models import (Blueprint, Card, Category, CollectionItem, Expansion, Listing, Order, OrderItem, Shipment)
from .test_dealer_api import COUNTERSPELL, SHEOLDRED, DealerTestCase
from .tests import game


class ReportsTest(DealerTestCase):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.sheoldred = self.item(SHEOLDRED, quantity=2, cost=Decimal("10.00"))
        self.counterspell = self.item(COUNTERSPELL)
        pokemon = game("pokemon")
        sv1 = Expansion.objects.create(game=pokemon, code="sv1", name="Scarlet & Violet")
        single = Category.objects.create(game=pokemon, cm_id=1, name="Pokémon Single", is_single=True)
        Blueprint.objects.create(cm_product_id=700001, expansion=sv1, name="Pikachu", collector_number="025",
                                 category=single)
        card = Card.objects.create(game=pokemon, name="Pikachu", set_code="SV1", collector_number="025",
                                   cm_product_id=700001)
        self.pikachu = CollectionItem.objects.create(owner=self.other, card=card, quantity=1)
        self.n = 0

        a = self.order("cm", now - timedelta(days=2), total="50.00", fee="5.00", country="IT")
        self.line(a, self.sheoldred, "20.00", 2, listed_at=now - timedelta(days=10))
        self.line(a, self.counterspell, "10.00", 1)
        Shipment.objects.create(order=a, postage_cost=Decimal("3.00"))
        b = self.order("ct", now - timedelta(days=5), total="33.00", fee="3.00", net="30.00", country="DE")
        self.line(b, self.pikachu, "30.00", 1)
        c = self.order("ct", now - timedelta(days=3), total="99.00", state="canceled")
        self.line(c, self.pikachu, "99.00", 1)
        d = self.order("cm", now - timedelta(days=40), total="20.00", net="20.00")
        self.line(d, self.counterspell, "20.00", 1)

    def order(self, market, paid_at, total, fee=None, net=None, state="paid", country=""):
        self.n += 1
        return Order.objects.create(owner=self.other, market=market, market_order_id=str(self.n), code=f"o{self.n}",
                                    state=state, paid_at=paid_at, total=Decimal(total),
                                    market_fee=Decimal(fee) if fee else None, net=Decimal(net) if net else None,
                                    ship_country_code=country)

    def line(self, order, item, price, qty, listed_at=None):
        listing, _ = Listing.objects.get_or_create(item=item, market=order.market,
                                                   defaults={"price": Decimal(price), "listed_at": listed_at})
        self.n += 1
        return OrderItem.objects.create(order=order, market_item_id=str(self.n), listing=listing, name=item.card.name,
                                        price=Decimal(price), quantity=qty)

    def report(self, **params):
        resp = self.client.get(reverse("ledger:api_reports"), params)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def test_kpis_and_range(self):
        data = self.report()  # Magic by default: the cm order (the ct one is Pokémon)
        today = timezone.localdate()
        self.assertEqual(data["range"], {"from": str(today - timedelta(days=29)), "to": str(today),
                                         "prev_from": str(today - timedelta(days=59)),
                                         "prev_to": str(today - timedelta(days=30))})
        self.assertEqual(data["kpis"], {"net_sales": "45.00", "net_sales_prev": "20.00", "gross_profit": "16.00",
                                        "profit_coverage_pct": "80.0", "orders": 1, "avg_basket": "45.00",
                                        "fees": "5.00", "postage": "3.00"})
        self.assertEqual(sum(Decimal(w["net_sales"]) for w in data["weeks"]), Decimal("45.00"))
        monday = today - timedelta(days=today.weekday())
        self.assertEqual(data["weeks"][-1]["week_start"], monday.isoformat())

    def test_breakdowns(self):
        data = self.report()
        self.assertEqual(data["by_market"], [
            {"market": "cm", "share_pct": "100.0", "net_sales": "45.00", "orders": 1, "fees": "5.00",
             "margin_pct": "44.4"},
            {"market": "ct", "share_pct": "0.0", "net_sales": "0.00", "orders": 0, "fees": "0.00",
             "margin_pct": None},
        ])
        sets = data["by_set"]
        self.assertEqual([(s["name"], s["game"], s["units"], s["net_sales"]) for s in sets],
                         [("Dominaria United", "mtg", 2, "36.00"), ("Dominaria Remastered", "mtg", 1, "9.00")])
        self.assertEqual((sets[0]["profit"], sets[0]["margin_pct"], sets[0]["days_to_sell"]), ("16.00", "44.4", 8.0))
        pokemon = self.report(game="pokemon")["by_set"]
        self.assertEqual([(s["name"], s["profit"], s["margin_pct"], s["days_to_sell"]) for s in pokemon],
                         [("Scarlet & Violet", None, None, None)])
        self.assertEqual(data["by_country"], [{"code": "IT", "orders": 1, "share_pct": "100.0"}])

    def test_filters(self):
        self.assertEqual(self.report(market="ct")["kpis"]["net_sales"], "0.00")  # Magic: only on cm
        self.assertEqual(self.report(market="ct", game="pokemon")["kpis"]["net_sales"], "30.00")
        mtg = self.report(game="mtg")
        self.assertEqual((mtg["kpis"]["net_sales"], mtg["kpis"]["orders"], mtg["kpis"]["net_sales_prev"]),
                         ("45.00", 1, "20.00"))
        self.assertEqual(self.report(game="pokemon")["kpis"]["net_sales"], "30.00")
        today = timezone.localdate()
        narrow = self.report(**{"from": str(today - timedelta(days=3)), "to": str(today)})
        self.assertEqual((narrow["kpis"]["net_sales"], narrow["range"]["prev_to"], narrow["range"]["prev_from"]),
                         ("45.00", str(today - timedelta(days=4)), str(today - timedelta(days=7))))
        self.assertEqual(narrow["kpis"]["net_sales_prev"], "0.00")  # the earlier order is Pokémon

    def test_game_share_of_a_mixed_order(self):
        mixed = self.order("ct", timezone.now() - timedelta(days=1), total="40.00", net="40.00", fee="4.00")
        self.line(mixed, self.pikachu, "30.00", 1)
        listing = Listing.objects.create(item=self.counterspell, market="ct", price=Decimal("10.00"))
        OrderItem.objects.create(order=mixed, market_item_id="mix", listing=listing, name="Counterspell",
                                 price=Decimal("10.00"), quantity=1)
        data = self.report(game="mtg")
        self.assertEqual((data["kpis"]["net_sales"], data["kpis"]["orders"], data["kpis"]["fees"]),
                         ("55.00", 2, "6.00"))  # 45 + 40 × 10/40; fees 5 + 4 × 1/4
        self.assertNotIn("Scarlet & Violet", [s["name"] for s in data["by_set"]])

    def test_validation(self):
        url = reverse("ledger:api_reports")
        for params in ({"from": "2026-12-01", "to": "2026-11-01"}, {"from": "tomorrow"}, {"market": "ebay"},
                       {"game": "chess"}, {"game": "all"}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get(url, params).status_code, 400)

    def test_sets_csv(self):
        resp = self.client.get(reverse("ledger:api_reports_sets_csv"), {"game": "mtg"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment;", resp["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(resp.content.decode())))
        self.assertEqual(rows[0], ["name", "code", "game", "units", "net_sales", "profit", "margin_pct",
                                   "days_to_sell"])
        self.assertEqual([r[0] for r in rows[1:]], ["Dominaria United", "Dominaria Remastered"])
        self.assertEqual(rows[1][3:], ["2", "36.00", "16.00", "44.4", "8.0"])

    def test_other_users_orders_are_not_counted(self):
        self.client.force_login(self.user)
        self.assertNotEqual(self.report()["kpis"]["net_sales"], "75.00")
