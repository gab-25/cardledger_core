"""Listings, orders and conflicts become CardTrader's (migrations 0036-0038), and users with a CardTrader token get
a CardTrader connection."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

AUTH = ("auth", "0012_alter_user_first_name_max_length")
BEFORE = [("ledger", "0035_alter_synclog_level_missingcardreport"), AUTH]
AFTER = [("ledger", "0038_markets_cleanup"), AUTH]


class MarketsMigrationTest(TransactionTestCase):
    def setUp(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(BEFORE)
        self.apps = self.executor.loader.project_state(BEFORE).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_existing_rows_become_cardtrader(self):
        get = self.apps.get_model
        game, _ = get("ledger", "Game").objects.get_or_create(slug="mtg", defaults={"name": "Magic", "cm_id": 1})
        User = get("auth", "User")
        gab, ami, eve = (User.objects.create(username=n) for n in ("gab", "ami", "eve"))
        card = get("ledger", "Card").objects.create(game=game, name="Ragavan", set_code="MH2")
        Item = get("ledger", "CollectionItem")
        item = Item.objects.create(owner=gab, card=card, quantity=2)
        other_item = Item.objects.create(owner=gab, card=card, quantity=1, condition="LP")
        Listing = get("ledger", "Listing")
        published = Listing.objects.create(item=item, price="10.00", status="published", ct_product_id=4242)
        pending = Listing.objects.create(item=other_item, price="5.00", status="to_publish")
        Order = get("ledger", "Order")
        order = Order.objects.create(owner=gab, ct_id=900, code="c900", state="paid")
        line = get("ledger", "OrderItem").objects.create(order=order, ct_id=9001, ct_product_id=4242,
                                                         listing=published, name="Ragavan")
        Service = get("ledger", "SyncService")
        now = timezone.now()
        service = Service.objects.create(user=gab, name="CardTrader", api_token="tok", last_sync=now,
                                         orders_synced_at=now)
        Service.objects.create(user=ami, name="CardTrader", api_token="tok2")  # never synced
        Service.objects.create(user=eve, name="CardTrader", api_token="")  # no token
        get("ledger", "SyncLog").objects.create(owner=gab, level="error", message="CardTrader: sync failed (boom)")
        conflict = get("ledger", "SyncConflict").objects.create(service=service, item=item, field="price",
                                                                local_value="1", remote_value="2")

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(AFTER)
        get = executor.loader.project_state(AFTER).apps.get_model

        Listing = get("ledger", "Listing")
        published = Listing.objects.get(pk=published.pk)
        self.assertEqual((published.market, published.market_listing_id, published.status), ("ct", "4242", "published"))
        self.assertIsNotNone(published.listed_at)
        pending = Listing.objects.get(pk=pending.pk)
        self.assertEqual((pending.market, pending.market_listing_id), ("ct", ""))
        self.assertIsNone(pending.listed_at)
        order = get("ledger", "Order").objects.get(pk=order.pk)
        self.assertEqual((order.market, order.market_order_id), ("ct", "900"))
        line = get("ledger", "OrderItem").objects.get(pk=line.pk)
        self.assertEqual((line.market_item_id, line.market_listing_id, line.listing_id), ("9001", "4242", published.pk))
        self.assertEqual(get("ledger", "SyncConflict").objects.get(pk=conflict.pk).market, "ct")

        conns = {c.user.username: c for c in get("ledger", "MarketConnection").objects.select_related("user")}
        self.assertEqual(set(conns), {"gab", "ami"})
        self.assertEqual((conns["gab"].market, conns["gab"].status), ("ct", "error"))
        self.assertEqual(conns["gab"].last_sync, now)
        self.assertEqual(conns["gab"].orders_synced_at, now)
        self.assertIn("boom", conns["gab"].last_error)
        self.assertEqual((conns["ami"].status, conns["ami"].last_sync), ("never", None))

        # a second listing of the same item, on Cardmarket, is now possible
        Listing.objects.create(item_id=published.item_id, market="cm", price="9.00")
        self.assertEqual(Listing.objects.filter(item_id=published.item_id).count(), 2)


class NoTokenMigrationTest(TransactionTestCase):
    """0039 drops SyncService (and the CardTrader tokens with it), keeping its sync times on the CardTrader
    connection."""

    BEFORE = [("ledger", "0038_markets_cleanup"), AUTH]
    AFTER = [("ledger", "0039_no_cardtrader_token"), AUTH]

    def setUp(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.BEFORE)
        self.apps = self.executor.loader.project_state(self.BEFORE).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_tokens_go_connections_stay(self):
        get = self.apps.get_model
        User = get("auth", "User")
        gab, ami = User.objects.create(username="gab"), User.objects.create(username="ami")
        now = timezone.now()
        Service = get("ledger", "SyncService")
        Service.objects.create(user=gab, name="CardTrader", api_token="tok", last_sync=now, orders_synced_at=now)
        Service.objects.create(user=ami, name="CardTrader", api_token="tok2", last_sync=now)
        Conn = get("ledger", "MarketConnection")
        Conn.objects.create(user=gab, market="ct", status="ok")

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(self.AFTER)
        apps = executor.loader.project_state(self.AFTER).apps

        with self.assertRaises(LookupError):
            apps.get_model("ledger", "SyncService")
        conns = {c.user.username: c for c in apps.get_model("ledger", "MarketConnection").objects.select_related("user")}
        self.assertEqual(set(conns), {"gab"})  # ami had no connection: CardTrader stays off for them
        self.assertEqual((conns["gab"].status, conns["gab"].last_sync, conns["gab"].orders_synced_at), ("ok", now, now))
