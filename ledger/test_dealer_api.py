"""The dealer data: item cost and location, collection views and stats, bulk actions, overview, packing order."""

from datetime import timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from . import services
from .models import CardmarketPrice, CollectionItem, Listing, Order, OrderItem
from .test_game_api import pokemon_setup
from .tests import LedgerTestCase, add, cm_id, load_catalog

SHEOLDRED, COUNTERSPELL, SOLITUDE, RAGAVAN = cm_id(1301), cm_id(1201), cm_id(1002), cm_id(1001)


class DealerTestCase(LedgerTestCase):
    """self.other's own collection (empty at first) over the test catalog; self.user keeps the demo data."""

    def setUp(self):
        super().setUp()
        load_catalog()
        self.client.force_login(self.other)

    def guide(self, product, **prices):
        CardmarketPrice.objects.update_or_create(id_product=product, defaults=prices)

    def item(self, product, **fields):
        item_id = add(self, product, foil=fields.pop("foil", False), quantity=fields.get("quantity") or 1).json()["id"]
        rest = {k: v for k, v in fields.items() if k != "quantity" or not v}
        if rest:
            CollectionItem.objects.filter(pk=item_id).update(**rest)
        return CollectionItem.objects.get(pk=item_id)

    def collection(self, **params):
        resp = self.client.get(reverse("ledger:api_collection"), params)
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def names(self, **params):
        return sorted(r["name"] for r in self.collection(**params)["results"])


class CollectionDealerTest(DealerTestCase):
    def setUp(self):
        super().setUp()
        self.guide(SHEOLDRED, trend=Decimal("10.00"))
        self.guide(COUNTERSPELL, trend=Decimal("0.30"))
        self.guide(SOLITUDE, trend=Decimal("20.00"))
        # listed under the trend, with a cost and a location
        self.sheoldred = self.item(SHEOLDRED, quantity=2, cost=Decimal("4.00"), location="A3·12")
        Listing.objects.create(item=self.sheoldred, price=Decimal("9.00"), quantity=1)
        # bulk, never listed, added long ago
        self.counterspell = self.item(COUNTERSPELL, quantity=3, cost=Decimal("0.10"))
        CollectionItem.objects.filter(pk=self.counterspell.pk).update(created_at=timezone.now() - timedelta(days=100))
        # listed at the trend
        self.solitude = self.item(SOLITUDE, location="B1")
        Listing.objects.create(item=self.solitude, price=Decimal("20.00"), quantity=1)
        # no trend, its listing sold out: not listed
        self.ragavan = self.item(RAGAVAN, quantity=0)
        Listing.objects.create(item=self.ragavan, price=Decimal("50.00"), quantity=0, status=Listing.Status.SOLD)
        CollectionItem.objects.filter(pk=self.ragavan.pk).update(created_at=timezone.now() - timedelta(days=200))

    def test_row_fields(self):
        rows = {r["name"]: r for r in self.collection()["results"]}
        row = rows["Sheoldred, the Apocalypse"]
        self.assertEqual((row["trend"], row["price"], row["cost"], row["location"], row["listing_price"]),
                         ("10.00", "10.00", "4.00", "A3·12", "9.00"))
        self.assertEqual(row["added_at"][:10], timezone.now().isoformat()[:10])
        row = rows["Ragavan, Nimble Pilferer"]
        self.assertEqual((row["trend"], row["cost"], row["location"], row["listing_price"]), (None, None, "", None))

    def test_stats(self):
        stats = self.collection()["stats"]
        self.assertEqual(stats["cost_value"], "8.30")  # 2 × 4.00 + 3 × 0.10
        self.assertEqual((stats["below_trend"], stats["not_listed"], stats["stale"]), (1, 2, 1))

    def test_cost_value_without_costs_is_null(self):
        CollectionItem.objects.update(cost=None)
        self.assertIsNone(self.collection()["stats"]["cost_value"])

    def test_views(self):
        self.assertEqual(self.names(view="below_trend"), ["Sheoldred, the Apocalypse"])
        self.assertEqual(self.names(view="not_listed"), ["Counterspell", "Ragavan, Nimble Pilferer"])
        self.assertEqual(self.names(view="stale"), ["Counterspell"])
        self.assertEqual(self.names(view="bulk"), ["Counterspell"])
        self.assertEqual(len(self.names(view="nope")), 4)
        data = self.collection(view="bulk")
        self.assertEqual((data["view"], data["filtered"]), ("bulk", True))
        self.assertEqual(data["views"], {"all": 4, "below_trend": 1, "not_listed": 2, "stale": 1, "bulk": 1})

    def test_views_combine_with_filters(self):
        self.assertEqual(self.names(view="not_listed", q="counter"), ["Counterspell"])
        self.assertEqual(self.collection(q="counter")["views"],
                         {"all": 1, "below_trend": 0, "not_listed": 1, "stale": 1, "bulk": 1})

    def test_location_filter(self):
        self.assertEqual(self.names(location="A3·12"), ["Sheoldred, the Apocalypse"])
        self.assertEqual(self.names(location="A3"), [])
        self.assertTrue(self.collection(location="B1")["filtered"])

    def test_item_detail_and_edit(self):
        url = reverse("ledger:api_item", args=[self.sheoldred.pk])
        data = self.client.get(url).json()
        self.assertEqual((data["cost"], data["location"]), ("4.00", "A3·12"))
        body = {"condition": "NM", "language": "EN", "quantity": 2}
        self.assertEqual(self.client.put(url, body, content_type="application/json").status_code, 200)
        self.sheoldred.refresh_from_db()  # left out: unchanged
        self.assertEqual((self.sheoldred.cost, self.sheoldred.location), (Decimal("4.00"), "A3·12"))
        resp = self.client.put(url, {**body, "cost": "2.5", "location": "C9"}, content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.sheoldred.refresh_from_db()
        self.assertEqual((self.sheoldred.cost, self.sheoldred.location), (Decimal("2.50"), "C9"))
        self.client.put(url, {**body, "cost": "", "location": ""}, content_type="application/json")
        self.sheoldred.refresh_from_db()
        self.assertEqual((self.sheoldred.cost, self.sheoldred.location), (None, ""))

    def test_negative_cost_is_400(self):
        url = reverse("ledger:api_item", args=[self.sheoldred.pk])
        resp = self.client.put(url, {"condition": "NM", "language": "EN", "quantity": 2, "cost": "-1"},
                               content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("cost", resp.json())


class CsvDealerTest(DealerTestCase):
    def test_export_has_cost_and_location(self):
        self.item(SHEOLDRED, cost=Decimal("4.00"), location="A3")
        lines = self.client.get(reverse("ledger:api_export")).content.decode().splitlines()
        self.assertEqual(lines[0], "name,set,condition,language,foil,quantity,number,cost,location")
        self.assertTrue(lines[1].endswith(",4.00,A3"))

    def test_import_cost_and_location(self):
        upload = SimpleUploadedFile("c.csv", b"name,set,quantity,cost,location\nBolt,M10,4,1.25,A1\nShock,M10,1,,\n")
        self.assertEqual(self.client.post(reverse("ledger:api_import"), {"file": upload}).status_code, 200)
        bolt = CollectionItem.objects.get(card__name="Bolt")
        self.assertEqual((bolt.cost, bolt.location), (Decimal("1.25"), "A1"))
        shock = CollectionItem.objects.get(card__name="Shock")
        self.assertEqual((shock.cost, shock.location), (None, ""))
        # a blank cell leaves an existing value as it is
        upload = SimpleUploadedFile("c.csv", b"name,set,quantity,cost,location\nBolt,M10,5,,B2\n")
        self.client.post(reverse("ledger:api_import"), {"file": upload})
        bolt.refresh_from_db()
        self.assertEqual((bolt.quantity, bolt.cost, bolt.location), (5, Decimal("1.25"), "B2"))

    def test_import_rejects_bad_cost(self):
        upload = SimpleUploadedFile("c.csv", b"name,set,cost\nBolt,M10,-2\n")
        resp = self.client.post(reverse("ledger:api_import"), {"file": upload})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("invalid cost", resp.json()["file"][0])


class BulkTest(DealerTestCase):
    def setUp(self):
        super().setUp()
        self.guide(SHEOLDRED, trend=Decimal("10.00"))
        self.guide(COUNTERSPELL, trend=Decimal("3.33"))
        self.sheoldred = self.item(SHEOLDRED, quantity=2)
        self.counterspell = self.item(COUNTERSPELL)
        self.ragavan = self.item(RAGAVAN)  # no trend
        self.mine = CollectionItem.objects.owned_by(self.user).first()  # the admin's

    def bulk(self, ids, action, game=None, **fields):
        url = reverse("ledger:api_collection_bulk") + (f"?game={game}" if game else "")
        return self.client.post(url, {"ids": ids, "action": action, **fields}, content_type="application/json")

    def test_set_location(self):
        resp = self.bulk([self.sheoldred.pk, self.counterspell.pk, self.mine.pk], "set_location", location="A3·12")
        self.assertEqual(resp.json(), {"count": 2, "messages": [{"level": "success", "text": "Moved 2 items to A3·12."}]})
        self.assertEqual(set(CollectionItem.objects.filter(location="A3·12")), {self.sheoldred, self.counterspell})
        self.mine.refresh_from_db()
        self.assertEqual(self.mine.location, "")

    def test_delete(self):
        resp = self.bulk([self.sheoldred.pk, self.mine.pk], "delete")
        self.assertEqual(resp.json()["messages"][0]["text"], "Removed 1 item from the collection.")
        self.assertFalse(CollectionItem.objects.filter(pk=self.sheoldred.pk).exists())
        self.assertTrue(CollectionItem.objects.filter(pk=self.mine.pk).exists())

    def test_list_at_trend(self):
        resp = self.bulk([self.sheoldred.pk, self.counterspell.pk, self.ragavan.pk], "list", pct="-10")
        self.assertEqual(resp.json()["messages"], [
            {"level": "success", "text": "Listed 2 items at trend -10%."},
            {"level": "warning", "text": "Skipped 1 item without a Cardmarket trend."},
        ])
        listing = Listing.objects.get(item=self.sheoldred)
        self.assertEqual((listing.price, listing.quantity, listing.status), (Decimal("9.00"), 2, "to_publish"))
        self.assertEqual(Listing.objects.get(item=self.counterspell).price, Decimal("3.00"))  # 2.997 → 3.00
        self.assertFalse(Listing.objects.filter(item=self.ragavan).exists())

    def test_list_updates_a_published_listing(self):
        Listing.objects.create(item=self.sheoldred, price=Decimal("8.00"), quantity=1, status="published",
                               ct_product_id=4242)
        self.bulk([self.sheoldred.pk], "list", pct="5")
        listing = Listing.objects.get(item=self.sheoldred)
        self.assertEqual((listing.price, listing.quantity, listing.status, listing.ct_product_id),
                         (Decimal("10.50"), 1, "to_publish", 4242))

    def test_reprice_only_listed(self):
        Listing.objects.create(item=self.sheoldred, price=Decimal("8.00"), quantity=1, status="published")
        resp = self.bulk([self.sheoldred.pk, self.counterspell.pk], "reprice", pct="2")
        self.assertEqual(resp.json()["messages"], [
            {"level": "success", "text": "Repriced 1 item at trend +2%."},
            {"level": "warning", "text": "Skipped 1 item without a listing."},
        ])
        self.assertEqual(Listing.objects.get(item=self.sheoldred).price, Decimal("10.20"))
        self.assertFalse(Listing.objects.filter(item=self.counterspell).exists())

    def test_other_users_and_other_games_items_are_ignored(self):
        _, _, pikachu = pokemon_setup(self)  # self.user's, of Pokémon
        listed_before = Listing.objects.owned_by(self.user).count()
        resp = self.bulk([self.mine.pk, pikachu.pk], "list")
        self.assertEqual(resp.json()["messages"],
                         [{"level": "warning", "text": "None of the selected items is in this collection."}])
        self.assertEqual(Listing.objects.owned_by(self.user).count(), listed_before)
        self.client.force_login(self.user)
        self.assertEqual(self.bulk([pikachu.pk, self.sheoldred.pk], "delete").json()["count"], 0)
        self.assertEqual(self.bulk([pikachu.pk], "delete", game="pokemon").json()["count"], 1)

    def test_validation(self):
        self.assertEqual(self.bulk([], "delete").status_code, 400)
        self.assertEqual(self.bulk(["x"], "delete").json(), {"ids": ["A non-empty list of item ids."]})
        self.assertIn("action", self.bulk([self.sheoldred.pk], "burn").json())
        self.assertIn("pct", self.bulk([self.sheoldred.pk], "list", pct="-100").json())
        self.assertEqual(self.bulk([self.sheoldred.pk], "delete", game="chess").status_code, 400)

    def test_price_over_trend(self):
        self.assertEqual(services.price_over_trend(Decimal("1.00"), Decimal("12")), Decimal("1.10"))  # 1.12
        self.assertEqual(services.price_over_trend(Decimal("1.00"), Decimal("13")), Decimal("1.15"))  # 1.13
        self.assertEqual(services.price_over_trend(Decimal("0.02"), Decimal("0")), Decimal("0.05"))


class OverviewTest(DealerTestCase):
    def overview(self, **params):
        resp = self.client.get(reverse("ledger:api_overview"), params)
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def order(self, ct_id, days_ago, total, state="paid", item=None):
        """An order of `total`; with `item`, one line of it for the whole total (the order counts for its game)."""
        order = Order.objects.create(owner=self.other, ct_id=ct_id, code=f"o{ct_id}", state=state, total=total,
                                     paid_at=timezone.now() - timedelta(days=days_ago))
        if item is not None:
            listing, _ = Listing.objects.get_or_create(item=item, defaults={"price": total, "quantity": 1})
            OrderItem.objects.create(order=order, market_item_id=f"l{ct_id}", listing=listing, name=item.card.name,
                                     price=total, quantity=1)
        return order

    def test_empty(self):
        data = self.overview()
        self.assertEqual(len(data["revenue_weeks"]), 12)
        self.assertEqual({w["total"] for w in data["revenue_weeks"]}, {"0.00"})
        self.assertEqual((data["revenue_30d"], data["revenue_prev_30d"], data["orders_30d"], data["avg_basket_30d"],
                          data["gross_profit_30d"], data["movers"]), (None, None, 0, None, None, []))
        self.assertEqual(data["stock_by_set"], [])

    def test_revenue(self):
        mtg = self.item(SHEOLDRED, quantity=5)
        self.order(1, 0, Decimal("20.00"), item=mtg)
        self.order(2, 3, Decimal("10.00"), item=mtg)
        self.order(3, 40, Decimal("7.50"), item=mtg)
        self.order(4, 1, Decimal("99.00"), state="canceled", item=mtg)
        self.order(5, 1, Decimal("50.00"))  # no line of the game: not counted
        data = self.overview()
        self.assertEqual((data["revenue_30d"], data["revenue_prev_30d"], data["orders_30d"], data["avg_basket_30d"]),
                         ("30.00", "7.50", 2, "15.00"))
        weeks = data["revenue_weeks"]
        today = timezone.localdate()
        monday = today - timedelta(days=today.weekday())
        self.assertEqual(weeks[-1]["week_start"], monday.isoformat())
        self.assertEqual(weeks[0]["week_start"], (monday - timedelta(weeks=11)).isoformat())
        self.assertEqual(sum(Decimal(w["total"]) for w in weeks), Decimal("37.50"))
        pokemon = self.overview(game="pokemon")
        self.assertEqual((pokemon["revenue_30d"], pokemon["orders_30d"]), (None, 0))

    def test_an_order_counts_for_the_share_of_its_game_lines(self):
        order = self.order(1, 1, Decimal("33.00"), item=self.item(SHEOLDRED))  # one line 33.00
        order.items.update(price=Decimal("20.00"))
        OrderItem.objects.create(order=order, market_item_id="x", name="Unknown card", price=Decimal("10.00"))
        data = self.overview()  # 33.00 × 20 / 30
        self.assertEqual((data["revenue"], data["orders"], data["avg_basket"]), ("22.00", 1, "22.00"))
        self.assertEqual(sum(Decimal(w["total"]) for w in data["revenue_weeks"]), Decimal("22.00"))

    def test_gross_profit(self):
        sheoldred = self.item(SHEOLDRED, cost=Decimal("12.00"))
        counterspell = self.item(COUNTERSPELL)  # no cost: left out
        order = self.order(1, 2, Decimal("50.00"))
        for i, (item, price, qty) in enumerate([(sheoldred, "20.00", 2), (counterspell, "5.00", 1)]):
            listing = Listing.objects.create(item=item, price=Decimal(price), quantity=1)
            OrderItem.objects.create(order=order, market_item_id=i, listing=listing, name=item.card.name,
                                     price=Decimal(price), quantity=qty)
        old = self.order(2, 45, Decimal("20.00"))
        OrderItem.objects.create(order=old, market_item_id=9, listing=sheoldred.listing_for(), name="x", price=Decimal("20.00"))
        self.assertEqual(self.overview()["gross_profit_30d"], "16.00")

    def test_stock_by_set(self):
        self.guide(SHEOLDRED, trend=Decimal("10.00"))
        self.guide(COUNTERSPELL, trend=Decimal("2.00"))
        self.item(SHEOLDRED, quantity=3)
        self.item(COUNTERSPELL, quantity=1)
        stock = self.overview()["stock_by_set"]
        self.assertEqual([(s["code"], s["value"]) for s in stock][0], ("DMU", "30.00"))
        self.assertEqual(len(stock), 2)
        self.assertTrue(all(s["name"] for s in stock))
        self.assertEqual(self.overview(game="pokemon")["stock_by_set"], [])

    def test_movers(self):
        self.guide(SHEOLDRED, trend=Decimal("11.00"), avg7=Decimal("12.00"), avg30=Decimal("10.00"),
                   trend_foil=Decimal("6.00"), avg7_foil=Decimal("5.00"), avg30_foil=Decimal("10.00"))
        self.guide(COUNTERSPELL, trend=Decimal("1.00"), avg7=Decimal("1.01"), avg30=Decimal("1.00"))
        self.guide(SOLITUDE, trend=Decimal("1.00"), avg7=Decimal("9.00"), avg30=Decimal("1.00"))
        up = self.item(SHEOLDRED, quantity=2)
        down = self.item(SHEOLDRED, foil=True)
        flat = self.item(COUNTERSPELL)
        self.item(SOLITUDE, quantity=0)  # out of stock: not a mover
        self.item(RAGAVAN)  # no averages
        movers = self.overview()["movers"]
        self.assertEqual([m["id"] for m in movers], [down.pk, up.pk, flat.pk])
        self.assertEqual(movers[0], {"id": down.pk, "name": "Sheoldred, the Apocalypse", "set_code": "DMU",
                                     "quantity": 1, "trend": "6.00", "change_pct": "-50.0", "exposure": "-3.00"})
        self.assertEqual((movers[1]["change_pct"], movers[1]["exposure"]), ("+20.0", "4.40"))
        self.assertEqual(movers[2]["change_pct"], "+1.0")
        self.assertEqual(self.overview(game="pokemon")["movers"], [])

    def test_other_users_data_is_not_counted(self):
        self.client.force_login(self.user)
        before = self.overview()
        self.client.force_login(self.other)
        data = self.overview()
        self.assertTrue(CollectionItem.objects.owned_by(self.user).exists())  # the admin's demo collection
        self.assertEqual(before["period"], "30d")
        self.assertEqual((data["revenue_30d"], data["orders_30d"]), (None, 0))
        self.assertEqual(data["stock_by_set"], [])

    def test_unknown_game_and_all_are_400(self):
        for game in ("chess", "all"):  # one game at a time, never all of them
            self.assertEqual(self.client.get(reverse("ledger:api_overview"), {"game": game}).status_code, 400)


class PackingOrderTest(LedgerTestCase):
    def test_order_items_carry_location_sorted(self):
        order = Order.objects.owned_by(self.user).filter(state="paid").first()
        order.items.all().delete()
        items = list(CollectionItem.objects.owned_by(self.user)[:3])
        for i, (item, location, name) in enumerate([(items[0], "B2", "Alpha"), (items[1], "A1", "Zeta"),
                                                    (items[2], "", "Beta")]):
            CollectionItem.objects.filter(pk=item.pk).update(location=location)
            listing, _ = Listing.objects.get_or_create(item=item, defaults={"price": Decimal("1.00")})
            OrderItem.objects.create(order=order, market_item_id=100 + i, listing=listing, name=name)
        OrderItem.objects.create(order=order, market_item_id=200, name="Aardvark")  # not linked
        data = self.client.get(reverse("ledger:api_order", args=[order.pk])).json()
        self.assertEqual([(l["name"], l["location"]) for l in data["items"]],
                         [("Zeta", "A1"), ("Alpha", "B2"), ("Aardvark", ""), ("Beta", "")])
