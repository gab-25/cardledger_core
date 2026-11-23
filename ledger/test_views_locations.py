"""Saved collection views, storage locations (used/free, suggest) and the overview extras (period, market, game all,
sell-through, singles/sealed, alerts)."""

from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from . import purchases
from .models import CollectionItem, Listing, Location, Order, OrderItem, SavedView
from .test_dealer_api import COUNTERSPELL, SHEOLDRED, SOLITUDE, DealerTestCase
from .tests import cm_id, game

BOOSTER_BOX = cm_id(1390)


class SavedViewTest(DealerTestCase):
    url = reverse("ledger:api_collection_views")

    def create(self, name, query, **extra):
        resp = self.client.post(self.url + "?game=mtg", {"name": name, "query": query, **extra},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()["id"]

    def listed(self, game="mtg"):
        return [(v["name"], v["query"], v["position"]) for v in self.client.get(self.url, {"game": game}).json()["results"]]

    def test_crud_and_order(self):
        a = self.create("Bulk", "?view=bulk&page=3")
        b = self.create("DMU foils", "set=DMU&foil=1")
        self.create("First", "q=sol", position=1)
        self.assertEqual(self.listed(), [("First", "q=sol", 1), ("Bulk", "view=bulk", 2),
                                         ("DMU foils", "set=DMU&foil=1", 3)])
        self.assertEqual(self.listed("pokemon"), [])
        resp = self.client.patch(reverse("ledger:api_collection_view", args=[b]), {"position": 1, "name": "Foils"},
                                 content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self.listed(), [("Foils", "set=DMU&foil=1", 1), ("First", "q=sol", 2),
                                         ("Bulk", "view=bulk", 3)])
        self.client.delete(reverse("ledger:api_collection_view", args=[a]))
        self.assertEqual([p for _, _, p in self.listed()], [1, 2])
        self.assertEqual(self.client.post(self.url, {"name": " "}, content_type="application/json").status_code, 400)

    def test_only_own_views(self):
        SavedView.objects.create(user=self.user, game=game(), name="Theirs", query="view=stale")
        self.assertEqual(self.listed(), [])


class LocationTest(DealerTestCase):
    url = reverse("ledger:api_locations")

    def setUp(self):
        super().setUp()
        self.item(SHEOLDRED, quantity=4, location="A1")
        self.item(COUNTERSPELL, quantity=3, location="B2")  # no Location row
        self.item(SOLITUDE, quantity=1)

    def add(self, **body):
        return self.client.post(self.url, body, content_type="application/json")

    def test_used_free_and_unregistered_codes(self):
        self.assertEqual(self.add(code="A1", capacity=5, note="red box").status_code, 201)
        self.assertEqual(self.add(code="C3", capacity=100).status_code, 201)
        self.assertEqual(self.add(code="A1").status_code, 400)  # unique per user
        rows = self.client.get(self.url).json()["results"]
        self.assertEqual([(r["code"], r["capacity"], r["used"], r["free"], r["note"]) for r in rows],
                         [("A1", 5, 4, 1, "red box"), ("B2", None, 3, None, ""), ("C3", 100, 0, 100, "")])
        self.assertIsNone(rows[1]["id"])

    def test_rename_moves_items_and_delete(self):
        loc = Location.objects.get(pk=self.add(code="A1", capacity=10).json()["id"])
        resp = self.client.patch(reverse("ledger:api_location", args=[loc.pk]), {"code": "A9"},
                                 content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        loc.refresh_from_db()
        self.assertEqual((loc.code, loc.capacity), ("A9", 10))
        self.assertEqual(CollectionItem.objects.get(location="A9").card.cm_product_id, SHEOLDRED)
        self.client.delete(reverse("ledger:api_location", args=[loc.pk]))
        self.assertFalse(Location.objects.filter(pk=loc.pk).exists())
        self.assertTrue(CollectionItem.objects.filter(location="A9").exists())

    def test_suggest(self):
        suggest = reverse("ledger:api_locations_suggest")
        self.add(code="A1", capacity=5)  # 1 free
        self.add(code="C3", capacity=3)  # 3 free
        self.add(code="D4", capacity=3)  # 3 free (ties by code)
        self.assertEqual(self.client.get(suggest).json(), {"code": "C3"})
        self.assertEqual(self.client.get(suggest, {"copies": "3"}).json(), {"code": "C3"})
        self.assertEqual(self.client.get(suggest, {"copies": "4"}).json(), {"code": None})
        self.assertEqual(self.client.get(suggest, {"copies": "x"}).status_code, 400)

    def test_other_users_locations_are_hidden(self):
        Location.objects.create(user=self.user, code="SECRET", capacity=1)
        self.assertNotIn("SECRET", [r["code"] for r in self.client.get(self.url).json()["results"]])


class OverviewExtrasTest(DealerTestCase):
    def overview(self, **params):
        resp = self.client.get(reverse("ledger:api_overview"), params)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def order(self, n, days_ago, total, market="ct", state="paid", lines=()):
        order = Order.objects.create(owner=self.other, market=market, market_order_id=str(n), code=f"o{n}",
                                     state=state, total=Decimal(total), paid_at=timezone.now() - timedelta(days=days_ago))
        for i, (item, price, qty) in enumerate(lines):
            listing, _ = Listing.objects.get_or_create(item=item, market=market, defaults={"price": Decimal(price)})
            OrderItem.objects.create(order=order, market_item_id=f"{n}-{i}", listing=listing, name=item.card.name,
                                     price=Decimal(price), quantity=qty)
        return order

    def test_period_and_market(self):
        self.order(1, 2, "10.00", market="cm")
        self.order(2, 10, "20.00")
        self.order(3, 20, "40.00")
        week = self.overview(period="7d")
        self.assertEqual((week["period"], week["revenue"], week["revenue_30d"], week["orders"], week["revenue_prev"]),
                         ("7d", "10.00", "10.00", 1, "20.00"))
        month = self.overview()
        self.assertEqual((month["period"], month["revenue"], month["orders"], month["avg_basket"]),
                         ("30d", "70.00", 3, "23.33"))
        self.assertEqual(self.overview(market="ct")["revenue"], "60.00")
        for period in ("qtd", "ytd"):
            self.assertEqual(self.overview(period=period)["period"], period)
        for params in ({"period": "1y"}, {"market": "ebay"}, {"game": "chess"}):
            self.assertEqual(self.client.get(reverse("ledger:api_overview"), params).status_code, 400)

    def test_singles_sealed_and_sell_through(self):
        sheoldred = self.item(SHEOLDRED, quantity=3)
        box = self.item(BOOSTER_BOX, quantity=1)
        self.order(1, 1, "100.00", lines=[(sheoldred, "20.00", 2), (box, "60.00", 1)])
        data = self.overview()
        totals = {k: sum(Decimal(w[k]) for w in data["revenue_weeks"]) for k in ("total", "singles", "sealed")}
        self.assertEqual(totals, {"total": Decimal("100.00"), "singles": Decimal("40.00"), "sealed": Decimal("60.00")})
        self.assertEqual(set(data["revenue_weeks"][0]), {"week_start", "total", "singles", "sealed"})
        # 3 sold, 4 in stock now: 3 / (4 + 3)
        self.assertEqual(data["sell_through"], "42.9")
        # previous 30 days: nothing sold, stock at its end = 4 + 3 - 4 added = 3
        self.assertEqual(data["sell_through_prev"], "0.0")
        self.assertEqual(self.overview(game="pokemon")["sell_through"], None)

    def test_game_all(self):
        self.guide(SHEOLDRED, trend=Decimal("11.00"), avg7=Decimal("12.00"), avg30=Decimal("10.00"))
        self.item(SHEOLDRED)
        self.assertEqual(len(self.overview(game="all")["movers"]), 1)
        self.assertEqual(len(self.overview(game="pokemon")["movers"]), 0)

    def test_alerts(self):
        self.assertEqual(self.overview()["alerts"], [])
        self.guide(SHEOLDRED, trend=Decimal("10.00"))
        self.guide(SOLITUDE, trend=Decimal("20.00"))
        lot = purchases.create(self.other, bought_on=timezone.localdate(), price_paid=Decimal("5"))
        sheoldred = self.item(SHEOLDRED, quantity=2, location="A1")
        Listing.objects.create(item=sheoldred, price=Decimal("8.00"), quantity=2)
        solitude = self.item(SOLITUDE, quantity=3, lot=lot, location="A1")
        CollectionItem.objects.filter(pk=solitude.pk).update(created_at=timezone.now() - timedelta(days=100))
        Location.objects.create(user=self.other, code="A1", capacity=5)
        Order.objects.create(owner=self.other, market="ct", market_order_id="9", code="o9", state="paid",
                             paid_at=timezone.now() - timedelta(days=10))
        alerts = {a["kind"]: a for a in self.overview()["alerts"]}
        self.assertEqual(list(alerts), ["orders_to_pack", "below_trend", "not_listed", "stale", "locations_full"])
        self.assertEqual((alerts["orders_to_pack"]["title"], alerts["orders_to_pack"]["tone"],
                          alerts["orders_to_pack"]["href"]), ("1 order to pack", "danger", "/orders?tab=to_pack"))
        self.assertTrue(alerts["orders_to_pack"]["sub"].startswith("1 overdue · 0 due today · oldest 10 days"))
        self.assertEqual((alerts["below_trend"]["title"], alerts["below_trend"]["sub"], alerts["below_trend"]["href"]),
                         ("1 listing below trend", "avg −20.0% · €4.00 under trend", "/mtg/?view=below_trend"))
        self.assertEqual((alerts["not_listed"]["title"], alerts["not_listed"]["sub"]),
                         ("3 copies not listed", "€60.00 at trend · 3 from PL-0001"))
        self.assertEqual((alerts["stale"]["title"], alerts["stale"]["href"]),
                         ("1 item unsold for 90+ days", "/mtg/?view=stale"))
        self.assertEqual((alerts["locations_full"]["sub"], alerts["locations_full"]["href"]),
                         ("A1 5/5", "/mtg/?location=A1"))
        self.assertEqual(self.overview(market="cm")["alerts"][0]["kind"], "below_trend")
        self.assertEqual([a["kind"] for a in self.overview(game="pokemon")["alerts"]],
                         ["orders_to_pack", "locations_full"])
        self.assertEqual({a["href"] for a in self.overview(game="all")["alerts"] if a["kind"] == "stale"},
                         {"/mtg/?view=stale"})
