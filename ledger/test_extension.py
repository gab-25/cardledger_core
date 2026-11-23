"""Markets and the browser extension: pairing, token auth, plan, report, shipped, and the per-market sale API."""

import uuid
from decimal import Decimal

from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from . import extension
from .models import (Card, CollectionItem, ExtensionPairing, ExtensionToken, Listing, MarketConnection, Order, SyncConflict,
                     SyncLog)
from .tests import LedgerTestCase


class ExtensionTestCase(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.token = self.pair_and_claim()
        self.ext = Client(HTTP_AUTHORIZATION=f"Token {self.token}")

    def pair_and_claim(self, client=None, name="Chrome"):
        client = client or self.client
        code = client.post(reverse("ledger:api_extension_pair")).json()["code"]
        resp = Client().post(reverse("ledger:api_extension_claim"), {"code": code, "name": name},
                             content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["token"]

    def plan(self, market, client=None):
        resp = (client or self.ext).get(reverse("ledger:api_extension_plan"), {"market": market})
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def report(self, data, client=None):
        data = {"run_id": str(uuid.uuid4()), "started_at": timezone.now().isoformat(),
                "finished_at": timezone.now().isoformat(), **data}
        return (client or self.ext).post(reverse("ledger:api_extension_report"), data,
                                         content_type="application/json")

    def listing(self, name, market="ct"):
        return Listing.objects.get(item__owner=self.user, item__card__name=name, market=market)

    def cm_listing(self, name, **fields):
        item = CollectionItem.objects.get(owner=self.user, card__name=name)
        return Listing.objects.create(item=item, market="cm", price=fields.pop("price", Decimal("5.00")),
                                      quantity=fields.pop("quantity", 1), **fields)


class PairingTest(ExtensionTestCase):
    def test_pair_and_claim(self):
        resp = self.client.post(reverse("ledger:api_extension_pair")).json()
        self.assertRegex(resp["code"], r"^[A-Z]{4}-\d{4}$")
        self.assertIn("expires_at", resp)
        claimed = Client().post(reverse("ledger:api_extension_claim"),
                                {"code": resp["code"].lower().replace("-", " "), "name": "Firefox"},
                                content_type="application/json").json()
        self.assertEqual(claimed["user"], {"username": "gab", "full_name": ""})
        self.assertTrue(ExtensionToken.objects.filter(user=self.user, name="Firefox").exists())
        self.assertNotIn(claimed["token"], ExtensionToken.objects.values_list("key", flat=True))  # hashed
        again = Client().post(reverse("ledger:api_extension_claim"), {"code": resp["code"]},
                              content_type="application/json")
        self.assertEqual(again.status_code, 400)
        self.assertIn("code", again.json())

    def test_expired_and_replaced_codes(self):
        first = self.client.post(reverse("ledger:api_extension_pair")).json()["code"]
        second = self.client.post(reverse("ledger:api_extension_pair")).json()["code"]
        claim = lambda code: Client().post(reverse("ledger:api_extension_claim"), {"code": code},
                                           content_type="application/json")
        self.assertEqual(claim(first).status_code, 400)  # replaced by the newer one
        ExtensionPairing.objects.filter(code=second).update(expires_at=timezone.now())
        self.assertEqual(claim(second).status_code, 400)

    def test_claim_is_rate_limited(self):
        cache.clear()
        anon = Client()
        for _ in range(10):
            self.assertEqual(anon.post(reverse("ledger:api_extension_claim"), {"code": "AAAA-0000"},
                                       content_type="application/json").status_code, 400)
        resp = anon.post(reverse("ledger:api_extension_claim"), {"code": "AAAA-0000"},
                         content_type="application/json")
        self.assertEqual(resp.status_code, 429)

    def test_pairing_turns_both_markets_on(self):
        me = self.client.get(reverse("accounts:api_me")).json()
        markets = {m["code"]: m for m in me["markets"]}
        self.assertEqual(set(markets), {"cm", "ct"})
        self.assertEqual((markets["cm"]["connected"], markets["cm"]["status"], markets["cm"]["handle"]),
                         (True, "never", ""))
        self.assertEqual((markets["ct"]["connected"], markets["ct"]["status"]), (True, "ok"))  # synced before
        self.assertEqual(me["extension"]["paired"], True)
        self.client.force_login(self.other)
        me = self.client.get(reverse("accounts:api_me")).json()
        self.assertEqual({m["code"]: m["status"] for m in me["markets"]}, {"cm": "off", "ct": "off"})
        self.assertEqual(me["extension"], {"paired": False, "last_seen_at": None})
        self.pair_and_claim()
        me = self.client.get(reverse("accounts:api_me")).json()
        self.assertEqual({m["code"]: (m["status"], m["connected"]) for m in me["markets"]},
                         {"cm": ("never", True), "ct": ("never", True)})

    def test_tokens_list_and_revoke(self):
        data = self.client.get(reverse("ledger:api_extension_tokens")).json()
        [row] = data["results"]
        self.assertEqual(set(row), {"id", "name", "created_at", "last_used_at", "revoked"})
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("ledger:api_extension_tokens")).json()["results"], [])
        self.assertEqual(self.client.delete(reverse("ledger:api_extension_token", args=[row["id"]])).status_code, 404)
        self.client.force_login(self.user)
        resp = self.client.delete(reverse("ledger:api_extension_token", args=[row["id"]]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.ext.get(reverse("ledger:api_extension_status")).status_code, 401)


class TokenAuthTest(ExtensionTestCase):
    def test_token_only_on_extension_endpoints(self):
        resp = self.ext.get(reverse("ledger:api_extension_status"))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["user"]["username"], "gab")
        self.assertEqual([m["code"] for m in data["markets"]], ["cm", "ct"])
        self.assertEqual(data["pending"], {"cm": 0, "ct": 3})
        self.assertIsNotNone(ExtensionToken.objects.get(user=self.user).last_used_at)
        # the token opens nothing else, and the session does not open the extension's endpoints
        self.assertEqual(self.ext.get(reverse("accounts:api_me")).status_code, 403)
        self.assertEqual(self.ext.get(reverse("ledger:api_collection")).status_code, 403)
        self.assertIn(self.client.get(reverse("ledger:api_extension_status")).status_code, (401, 403))
        bad = Client(HTTP_AUTHORIZATION="Token nope")
        self.assertEqual(bad.get(reverse("ledger:api_extension_status")).status_code, 401)

    def test_no_cardtrader_token_endpoint(self):
        self.assertEqual(self.ext.get("/api/extension/cardtrader-token/").status_code, 404)


class PlanTest(ExtensionTestCase):
    def test_plan_lists_changes_per_market(self):
        ct = self.plan("ct")
        self.assertEqual(ct["market"], "ct")
        self.assertEqual({c["item"]["name"] for c in ct["changes"]},
                         {"Sheoldred, the Apocalypse", "Fable of the Mirror-Breaker", "Counterspell"})
        change = next(c for c in ct["changes"] if c["item"]["name"] == "Counterspell")
        self.assertEqual((change["action"], change["market_listing_id"], change["price"], change["quantity"]),
                         ("create", "", "0.80", 1))
        self.assertEqual(set(change["item"]), {"id", "name", "set_code", "collector_number", "cm_product_id",
                                               "ct_blueprint_id", "game", "condition", "language", "foil",
                                               "is_product"})
        self.assertEqual(self.plan("cm")["changes"], [])
        self.assertEqual(self.ext.get(reverse("ledger:api_extension_plan"), {"market": "xx"}).status_code, 400)

    def test_actions(self):
        create = self.cm_listing("Thoughtseize")
        update = self.cm_listing("Wrenn and Six", market_listing_id="A1")
        paused = self.cm_listing("Orcish Bowmasters", market_listing_id="A2", status="paused")
        removed = self.cm_listing("The One Ring", market_listing_id="A3", status="published", pending_delete=True)
        self.cm_listing("Ragavan, Nimble Pilferer", market_listing_id="A4", status="published")  # nothing to do
        actions = {c["listing_id"]: c["action"] for c in self.plan("cm")["changes"]}
        self.assertEqual(actions, {create.pk: "create", update.pk: "update", paused.pk: "delete",
                                   removed.pk: "delete"})

    def test_other_users_listings_are_not_planned(self):
        self.client.force_login(self.other)
        other = Client(HTTP_AUTHORIZATION=f"Token {self.pair_and_claim()}")
        self.assertEqual(self.plan("ct", client=other)["changes"], [])


class ReportTest(ExtensionTestCase):
    def test_results_are_applied(self):
        counterspell, fable = self.listing("Counterspell"), self.listing("Fable of the Mirror-Breaker")
        removed = self.cm_listing("The One Ring", market_listing_id="A3", status="published", pending_delete=True)
        theirs_item = CollectionItem.objects.create(owner=self.other, card=counterspell.item.card, quantity=1)
        theirs = Listing.objects.create(item=theirs_item, price="1.00")
        resp = self.report({"market": "ct", "results": [
            {"listing_id": counterspell.pk, "ok": True, "market_listing_id": "555"},
            {"listing_id": fable.pk, "ok": False, "error": "blueprint not found"},
            {"listing_id": theirs.pk, "ok": True, "market_listing_id": "999"},
            {"listing_id": removed.pk, "ok": True},  # a Cardmarket listing in a CardTrader run: ignored
        ]})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json(), {"applied": 2, "conflicts": 0, "orders_new": 0, "unmatched": []})
        counterspell.refresh_from_db()
        self.assertEqual((counterspell.status, counterspell.market_listing_id), ("published", "555"))
        self.assertIsNotNone(counterspell.listed_at)
        fable.refresh_from_db()
        self.assertEqual((fable.status, fable.error_message), ("error", "blueprint not found"))
        theirs.refresh_from_db()
        self.assertEqual((theirs.status, theirs.market_listing_id), ("to_publish", ""))
        self.assertTrue(Listing.objects.filter(pk=removed.pk).exists())
        self.assertTrue(SyncLog.objects.filter(owner=self.user, message__contains="Fable of the Mirror-Breaker "
                                                                                  "not synced").exists())

    def test_delete_results(self):
        removed = self.cm_listing("The One Ring", market_listing_id="A3", status="published", pending_delete=True)
        paused = self.cm_listing("Orcish Bowmasters", market_listing_id="A2", status="paused")
        self.report({"market": "cm", "results": [{"listing_id": removed.pk, "ok": True},
                                                 {"listing_id": paused.pk, "ok": True}]})
        self.assertFalse(Listing.objects.filter(pk=removed.pk).exists())
        paused.refresh_from_db()
        self.assertEqual((paused.status, paused.market_listing_id), ("paused", ""))
        conn = MarketConnection.objects.get(user=self.user, market="cm")
        self.assertEqual(conn.status, "ok")
        self.assertIsNotNone(conn.last_sync)

    def order(self, **fields):
        return {"market_order_id": "1001", "state": "paid", "buyer_username": "bob", "buyer_name": "Bob Rossi",
                "ship_name": "Bob Rossi", "ship_city": "Roma", "ship_country_code": "IT", "shipping_method": "Letter",
                "paid_at": timezone.now().isoformat(), "items_total": "10.00", "shipping_cost": "1.50",
                "market_fee": "0.50", "total": "11.50", "currency": "EUR",
                "items": [{"market_item_id": "L1", "market_listing_id": "C1", "name": "Sheoldred, the Apocalypse",
                           "expansion": "DMU", "condition": "LP", "language": "IT", "quantity": 1, "price": "10.00"}],
                **fields}

    def test_orders_decrement_stock_once_and_share_it(self):
        sheoldred = self.listing("Sheoldred, the Apocalypse")  # CardTrader: 2 of 2 copies, to publish
        sheoldred.status, sheoldred.market_listing_id = Listing.Status.PUBLISHED, "77"
        sheoldred.save()
        cm = self.cm_listing("Sheoldred, the Apocalypse", market_listing_id="C1", status="published", quantity=2)
        data = {"run_id": str(uuid.uuid4()), "market": "cm", "orders": [self.order()]}
        resp = self.ext.post(reverse("ledger:api_extension_report"), data, content_type="application/json")
        self.assertEqual(resp.json()["orders_new"], 1)
        again = self.ext.post(reverse("ledger:api_extension_report"), data, content_type="application/json")
        self.assertEqual(again.json(), resp.json())  # same run: answered again, applied once
        self.report({"market": "cm", "orders": [self.order(state="sent")]})  # a later run: updated, not re-applied
        order = Order.objects.get(owner=self.user, market="cm", market_order_id="1001")
        self.assertEqual((order.state, order.buyer_name, order.net, order.size), ("sent", "Bob Rossi",
                                                                                 Decimal("11.00"), 1))
        self.assertEqual(order.items.get().listing, cm)
        cm.refresh_from_db()
        sheoldred.refresh_from_db()
        self.assertEqual((cm.quantity, cm.item.quantity), (1, 1))
        # shared stock: CardTrader's listing is capped to the copy left and queued for the next sync
        self.assertEqual((sheoldred.quantity, sheoldred.status), (1, "to_publish"))
        self.assertEqual({c["listing_id"]: c["action"] for c in self.plan("ct")["changes"]}[sheoldred.pk], "update")

    def test_last_copy_sold_removes_other_markets_listing(self):
        ragavan = self.listing("Ragavan, Nimble Pilferer")  # 1 copy, published on CardTrader
        ragavan.market_listing_id = "88"
        ragavan.save()
        self.cm_listing("Ragavan, Nimble Pilferer", market_listing_id="C1", status="published")
        self.report({"market": "cm", "orders": [self.order(items=[
            {"market_item_id": "L1", "market_listing_id": "C1", "name": "Ragavan", "quantity": 1, "price": "48.00"}])]})
        ragavan.refresh_from_db()
        self.assertTrue(ragavan.pending_delete)
        self.assertEqual({c["listing_id"]: c["action"] for c in self.plan("ct")["changes"]}[ragavan.pk], "delete")
        self.assertEqual(self.listing("Ragavan, Nimble Pilferer", "cm").status, "sold")

    def test_stock_conflicts_and_unmatched(self):
        known = self.cm_listing("Wrenn and Six", market_listing_id="A1", status="published", price=Decimal("29.90"))
        gone = self.cm_listing("Thoughtseize", market_listing_id="A2", status="published")
        resp = self.report({"market": "cm", "stock": [
            {"market_listing_id": "A1", "name": "Wrenn and Six", "quantity": 1, "price": "27.00"},
            {"market_listing_id": "Z9", "name": "Mystery", "set_code": "XXX", "condition": "NM", "language": "EN",
             "foil": False, "quantity": 3, "price": "1.00"},
        ]}).json()
        self.assertEqual(resp["conflicts"], 2)
        [row] = resp["unmatched"]
        self.assertEqual((row["market_listing_id"], row["name"], row["quantity"], row["price"], row["item_id"]),
                         ("Z9", "Mystery", 3, "1.00", None))
        conflicts = {(c.item_id, c.field): c for c in SyncConflict.objects.filter(market="cm", resolution="open")}
        self.assertEqual(set(conflicts), {(known.item_id, "price"), (gone.item_id, "status")})
        self.assertEqual(conflicts[(gone.item_id, "status")].remote_value, "Not on Cardmarket")
        sync = self.client.get(reverse("ledger:api_sync")).json()
        self.assertIn("Cardmarket", {c["service"] for c in sync["conflicts"]})
        # keep remote applies to the Cardmarket listing
        self.client.post(reverse("ledger:api_resolve", args=[conflicts[(known.item_id, "price")].pk]),
                         {"keep": "remote"}, content_type="application/json")
        known.refresh_from_db()
        self.assertEqual(known.price, Decimal("27.00"))

    def test_cardtrader_blueprints_are_stored_on_cards(self):
        counterspell, ragavan = self.listing("Counterspell"), self.listing("Ragavan, Nimble Pilferer")
        ragavan.market_listing_id = "88"
        ragavan.save()
        taken = Card.objects.create(game=counterspell.item.card.game, name="Other", set_code="XXX",
                                    ct_blueprint_id=3003)
        fable = self.listing("Fable of the Mirror-Breaker")
        resp = self.report({"market": "ct", "results": [
            {"listing_id": counterspell.pk, "ok": True, "market_listing_id": "555", "ct_blueprint_id": 1201},
            {"listing_id": fable.pk, "ok": True, "market_listing_id": "556", "ct_blueprint_id": 3003},
        ], "stock": [
            {"market_listing_id": "555", "ct_blueprint_id": 1201, "quantity": 4, "price": "0.80"},
            {"market_listing_id": "556", "quantity": 2, "price": "19.20"},
            {"market_listing_id": "88", "ct_blueprint_id": 1001, "quantity": 1, "price": "48.00"},
            {"market_listing_id": "999", "ct_blueprint_id": 4004, "name": "Unknown", "quantity": 1, "price": "1.00"},
        ]})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(Card.objects.get(pk=counterspell.item.card_id).ct_blueprint_id, 1201)
        self.assertEqual(Card.objects.get(pk=ragavan.item.card_id).ct_blueprint_id, 1001)  # from the stock
        self.assertIsNone(Card.objects.get(pk=fable.item.card_id).ct_blueprint_id)  # 3003 is another card's
        self.assertEqual(Card.objects.get(pk=taken.pk).ct_blueprint_id, 3003)
        self.assertFalse(Card.objects.filter(ct_blueprint_id=4004).exists())  # unmatched rows teach nothing
        self.assertTrue(SyncLog.objects.filter(owner=self.user, message__contains="blueprint 3003").exists())
        # a Cardmarket run never sets CardTrader blueprints
        thoughtseize = self.cm_listing("Thoughtseize")
        self.report({"market": "cm", "results": [{"listing_id": thoughtseize.pk, "ok": True,
                                                  "market_listing_id": "A1", "ct_blueprint_id": 5005}]})
        self.assertIsNone(Card.objects.get(pk=thoughtseize.item.card_id).ct_blueprint_id)

    def test_error_report_marks_the_market(self):
        self.report({"market": "ct", "error": "CardTrader said 401", "handle": "gabshop"})
        ct = {m["code"]: m for m in self.client.get(reverse("accounts:api_me")).json()["markets"]}["ct"]
        self.assertEqual((ct["status"], ct["handle"]), ("error", "gabshop"))
        self.assertEqual(MarketConnection.objects.get(user=self.user, market="ct").last_error, "CardTrader said 401")
        self.report({"market": "ct"})
        ct = {m["code"]: m for m in self.client.get(reverse("accounts:api_me")).json()["markets"]}["ct"]
        self.assertEqual(ct["status"], "ok")

    def test_invalid_report(self):
        self.assertEqual(self.report({"market": "xx"}).status_code, 400)
        resp = self.ext.post(reverse("ledger:api_extension_report"), {"market": "cm"}, content_type="application/json")
        self.assertEqual(resp.status_code, 400)


class ShippedTest(ExtensionTestCase):
    def test_marks_order_shipped(self):
        order = Order.objects.create(owner=self.user, market="cm", market_order_id="55", code="55", state="paid")
        resp = self.ext.post(reverse("ledger:api_extension_shipped"),
                             {"market": "cm", "market_order_id": "55", "tracking_code": "RR1"},
                             content_type="application/json")
        self.assertEqual(resp.json(), {"id": order.pk, "state": "sent", "tracking_code": "RR1"})
        order.refresh_from_db()
        self.assertIsNotNone(order.sent_at)
        # another user's order is not found
        Order.objects.create(owner=self.other, market="cm", market_order_id="56", code="56", state="paid")
        self.assertEqual(self.ext.post(reverse("ledger:api_extension_shipped"),
                                       {"market": "cm", "market_order_id": "56"},
                                       content_type="application/json").status_code, 404)

    def test_marks_cardtrader_orders_shipped(self):
        paid = Order.objects.owned_by(self.user).filter(market="ct", state="paid").first()
        ct0 = Order.objects.owned_by(self.user).get(market="ct", state="hub_pending")
        for order, tracking in ((paid, "RR9IT"), (ct0, "")):
            with self.subTest(state=order.state):
                resp = self.ext.post(reverse("ledger:api_extension_shipped"),
                                     {"market": "ct", "market_order_id": order.market_order_id,
                                      "tracking_code": tracking}, content_type="application/json")
                self.assertEqual(resp.status_code, 200, resp.content)
                self.assertEqual(resp.json()["state"], "sent")
                order.refresh_from_db()
                self.assertEqual(order.state, "sent")
                self.assertIsNotNone(order.sent_at)
        self.assertEqual(paid.tracking_code, "RR9IT")
        self.assertTrue(SyncLog.objects.filter(owner=self.user, message=f"CardTrader: order #{paid.code} shipped")
                        .exists())
        # the same id on the other market is not this order
        self.assertEqual(self.ext.post(reverse("ledger:api_extension_shipped"),
                                       {"market": "cm", "market_order_id": paid.market_order_id},
                                       content_type="application/json").status_code, 404)


class MarketSaleApiTest(ExtensionTestCase):
    def test_listings_filter_and_stats(self):
        self.cm_listing("Thoughtseize", status="published")
        self.cm_listing("The One Ring", market_listing_id="A3", status="published", pending_delete=True)
        data = self.client.get(reverse("ledger:api_listings")).json()
        self.assertEqual(data["market"], "")
        self.assertEqual(data["by_market"]["cm"]["active"], 1)  # the removed one is left out
        self.assertEqual(data["stats"]["active"], data["by_market"]["cm"]["active"] + data["by_market"]["ct"]["active"])
        row = data["results"][0]
        self.assertTrue({"id", "item_id", "market", "status", "error_message", "listed_at"} <= set(row))
        cm = self.client.get(reverse("ledger:api_listings"), {"market": "cm"}).json()
        self.assertEqual([(r["name"], r["market"]) for r in cm["results"]], [("Thoughtseize", "cm")])

    def test_listing_detail_put_delete_per_market(self):
        item = CollectionItem.objects.get(owner=self.user, card__name="Counterspell")
        url = reverse("ledger:api_listing", args=[item.pk])
        resp = self.client.put(url, {"market": "cm", "quantity": 2, "price": "0.90", "status": "to_publish"},
                               content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        data = self.client.get(url).json()
        self.assertEqual([(l["market"], l["price"], l["quantity"]) for l in data["listings"]],
                         [("cm", "0.90", 2), ("ct", "0.80", 1)])
        self.assertEqual(set(data["listings"][0]) >= {"market", "quantity", "price", "status", "error_message"}, True)
        cm = self.listing("Counterspell", "cm")
        cm.status, cm.market_listing_id = Listing.Status.PUBLISHED, "A9"
        cm.save()
        self.client.delete(url + "?market=cm")
        cm.refresh_from_db()
        self.assertTrue(cm.pending_delete)  # deleted on Cardmarket by the next sync
        self.assertEqual([l["market"] for l in self.client.get(url).json()["listings"]], ["ct"])
        self.assertEqual(self.client.delete(url + "?market=cm").status_code, 404)
        self.assertEqual(self.client.put(url, {"market": "zz", "quantity": 1, "price": "1", "status": "to_publish"},
                                         content_type="application/json").status_code, 400)

    def test_bulk(self):
        sheoldred = self.listing("Sheoldred, the Apocalypse")
        cm = self.cm_listing("Sheoldred, the Apocalypse", market_listing_id="C1", status="published")
        theirs_item = CollectionItem.objects.create(owner=self.other, card=sheoldred.item.card, quantity=1)
        theirs = Listing.objects.create(item=theirs_item, price="1.00", status="published")
        url = reverse("ledger:api_listings_bulk")
        resp = self.client.post(url, {"ids": [sheoldred.item_id, theirs_item.pk], "market": "cm", "action": "pause"},
                                content_type="application/json")
        self.assertEqual(resp.json()["count"], 1)
        cm.refresh_from_db()
        sheoldred.refresh_from_db()
        self.assertEqual((cm.status, sheoldred.status), ("paused", "to_publish"))
        self.client.post(url, {"ids": [sheoldred.item_id], "action": "resume"}, content_type="application/json")
        cm.refresh_from_db()
        self.assertEqual(cm.status, "to_publish")
        self.client.post(url, {"ids": [sheoldred.item_id, theirs_item.pk], "action": "delete"},
                         content_type="application/json")
        self.assertFalse(Listing.objects.filter(pk=sheoldred.pk).exists())  # never on CardTrader: gone
        cm.refresh_from_db()
        self.assertTrue(cm.pending_delete)
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, "published")
        self.assertEqual(self.client.post(url, {"ids": [], "action": "pause"},
                                          content_type="application/json").status_code, 400)

    def test_collection_rows_and_bulk_markets(self):
        self.cm_listing("Counterspell", price=Decimal("0.50"))
        rows = {r["name"]: r for r in self.client.get(reverse("ledger:api_collection")).json()["results"]}
        self.assertEqual(rows["Counterspell"]["listings"], [{"market": "cm", "status": "to_publish", "price": "0.50"},
                                                            {"market": "ct", "status": "to_publish", "price": "0.80"}])
        self.assertEqual(rows["Counterspell"]["listing_price"], "0.50")
        self.assertEqual(rows["The One Ring"]["listings"], [])

    def test_orders_market_filter_and_money(self):
        Order.objects.create(owner=self.user, market="cm", market_order_id="55", code="55", state="paid",
                             buyer_name="Bob", items_total="10.00", shipping_cost="1.00", market_fee="0.50",
                             total="11.00", net="10.50")
        data = self.client.get(reverse("ledger:api_orders"), {"market": "cm"}).json()
        self.assertEqual(data["market"], "cm")
        [row] = data["results"]
        self.assertEqual((row["market"], row["buyer_name"], row["items_total"], row["shipping_cost"],
                          row["market_fee"], row["net"]), ("cm", "Bob", "10.00", "1.00", "0.50", "10.50"))
        self.assertEqual({r["market"] for r in self.client.get(reverse("ledger:api_orders")).json()["results"]},
                         {"cm", "ct"})
