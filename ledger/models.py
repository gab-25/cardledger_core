import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


def _owner(related_name):
    """The user a personal row belongs to."""
    return models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name=related_name)


class OwnedQuerySet(models.QuerySet):
    """Rows belonging to one user; owner_path is the lookup from the model to its owner."""

    owner_path = "owner"

    def owned_by(self, user):
        return self.filter(**{self.owner_path: user})


def owned_manager(path):
    return type(f"OwnedBy{path.title().replace('__', '')}QuerySet", (OwnedQuerySet,), {"owner_path": path}).as_manager()


class SyncLogQuerySet(models.QuerySet):
    def visible_to(self, user):
        """The user's own entries plus the global ones (no owner, e.g. the catalog sync)."""
        return self.filter(Q(owner=user) | Q(owner__isnull=True))


class Condition(models.TextChoices):
    MINT = "M", "Mint"
    NEAR_MINT = "NM", "Near Mint"
    EXCELLENT = "EX", "Excellent"
    GOOD = "GD", "Good"
    LIGHT_PLAYED = "LP", "Light Played"
    PLAYED = "PL", "Played"
    POOR = "PO", "Poor"


class Language(models.TextChoices):
    EN = "EN", "English"
    IT = "IT", "Italian"
    DE = "DE", "German"
    FR = "FR", "French"
    ES = "ES", "Spanish"
    PT = "PT", "Portuguese"
    JP = "JP", "Japanese"
    KO = "KO", "Korean"
    ZHS = "ZHS", "Chinese (S)"
    RU = "RU", "Russian"


MAGIC = "mtg"


class Market(models.TextChoices):
    """A marketplace CardLedger sells on; both are synced only by the browser extension (the server never calls
    them)."""

    CARDMARKET = "cm", "Cardmarket"
    CARDTRADER = "ct", "CardTrader"


class Game(models.Model):
    """A trading card game of the catalog (Magic, Pokémon, ...) and its Cardmarket id."""

    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=100)
    cm_id = models.PositiveIntegerField("Cardmarket game", unique=True, help_text="Cardmarket's idGame")
    catalog_sha256 = models.CharField(
        max_length=64, blank=True, help_text="sha256 of the catalog file last imported (the sync skips the same file)",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @classmethod
    def by_slug(cls, slug):
        """The game with this slug; Game.DoesNotExist when there is none."""
        return cls.objects.get(slug=slug)


class GameVisionIndex(models.Model):
    """A game's vision index file (cardledger_vision), apart from Game so that loading a game never loads it."""

    game = models.OneToOneField(Game, on_delete=models.CASCADE, related_name="vision")
    data = models.BinaryField(editable=False)
    sha256 = models.CharField(max_length=64, help_text="sha256 of the file stored (the sync skips the same file)")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.game} vision index"


class Card(models.Model):
    """A specific printing of a card (name + set)."""

    game = models.ForeignKey(Game, on_delete=models.PROTECT, related_name="cards")
    name = models.CharField(max_length=200)
    set_code = models.CharField(max_length=40)
    collector_number = models.CharField(max_length=40, blank=True, default="")
    cm_product_id = models.PositiveIntegerField(
        "Cardmarket product", null=True, blank=True, db_index=True,
        help_text="Its printing in the catalog (Blueprint); the catalog sync keeps one card per product",
    )
    ct_blueprint_id = models.PositiveIntegerField(
        "CardTrader blueprint", null=True, blank=True,
        help_text="Reported by the extension's CardTrader sync (results or stock of a known listing)"
    )
    is_product = models.BooleanField(
        default=False, help_text="A sealed product or accessory (no condition or foil), not a card"
    )

    class Meta:
        ordering = ["name", "set_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["game", "set_code", "collector_number"], condition=~Q(collector_number=""),
                name="unique_card_set_number",
            ),
            models.UniqueConstraint(
                fields=["game", "name", "set_code"], condition=Q(collector_number=""),
                name="unique_card_legacy_name_set",
            ),
            models.UniqueConstraint(
                fields=["ct_blueprint_id"], condition=Q(ct_blueprint_id__isnull=False), name="unique_card_blueprint",
            ),
        ]

    def __str__(self):
        if self.collector_number:
            return f"{self.name} ({self.set_code} #{self.collector_number})"
        return f"{self.name} ({self.set_code})"


class CollectionItem(models.Model):
    """A stack of identical copies owned by the user."""

    class Status(models.TextChoices):
        COLLECTION = "collection", "In collection"
        FOR_SALE = "sale", "For sale"
        PENDING = "pending", "Sync pending"

    owner = _owner("collection_items")
    card = models.ForeignKey(Card, on_delete=models.CASCADE, related_name="items")
    condition = models.CharField(max_length=2, choices=Condition.choices, default=Condition.NEAR_MINT)
    language = models.CharField(max_length=3, choices=Language.choices, default=Language.EN)
    foil = models.BooleanField(default=False)
    quantity = models.PositiveIntegerField(default=1)
    cost = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True, help_text="Purchase price of one copy",
    )
    location = models.CharField(max_length=40, blank=True, help_text='Storage location, e.g. "A3·12"')
    lot = models.ForeignKey("PurchaseLot", null=True, blank=True, on_delete=models.SET_NULL, related_name="items",
                            help_text="The purchase lot the copies came from")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = owned_manager("owner")

    class Meta:
        ordering = ["card__name", "card__set_code"]

    def __str__(self):
        return f"{self.quantity}× {self.card} {self.condition} {self.language}"

    def listing_for(self, market=Market.CARDTRADER):
        """The item's listing on market (any status), or None; uses prefetched listings when there are."""
        return next((l for l in self.listings.all() if l.market == market), None)

    def active_listings(self):
        """The item's listings that are not sold nor removed (any market)."""
        return [l for l in self.listings.all() if l.is_active]

    @property
    def status(self):
        """In collection without an active listing; sync pending when one waits to be pushed; else for sale."""
        active = self.active_listings()
        if not active:
            return self.Status.COLLECTION
        if any(l.status == Listing.Status.TO_PUBLISH for l in active):
            return self.Status.PENDING
        return self.Status.FOR_SALE


class Listing(models.Model):
    """A sale listing of a collection item on one market (one per item and market)."""

    class Status(models.TextChoices):
        PUBLISHED = "published", "Published"
        TO_PUBLISH = "to_publish", "To publish"
        PAUSED = "paused", "Paused"  # kept locally, taken off the market (the next sync deletes it there)
        ERROR = "error", "Error"  # the last push failed (error_message); retried by the next sync
        SOLD = "sold", "Sold"

    item = models.ForeignKey(CollectionItem, on_delete=models.CASCADE, related_name="listings")
    market = models.CharField(max_length=2, choices=Market.choices, default=Market.CARDTRADER)
    market_listing_id = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text="The listing's id on the market (CardTrader product id, Cardmarket article id); blank until pushed",
    )
    quantity = models.PositiveIntegerField(default=1)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.TO_PUBLISH)
    error_message = models.CharField(max_length=300, blank=True)
    listed_at = models.DateTimeField(null=True, blank=True, help_text="When it was first published")
    pending_delete = models.BooleanField(
        default=False, help_text="Removed by the user while on the market: the next sync deletes it there, then here",
    )
    sold_at = models.DateTimeField(null=True, blank=True)
    last_step_at = models.DateTimeField(
        null=True, blank=True, help_text="When an over-time repricing rule last changed its price",
    )
    updated_at = models.DateTimeField(auto_now=True)

    objects = owned_manager("item__owner")

    class Meta:
        ordering = ["item__card__name", "market"]
        constraints = [models.UniqueConstraint(fields=["item", "market"], name="unique_listing_item_market")]

    def __str__(self):
        return f"{self.item.card} @ {self.price} ({self.market})"

    @property
    def is_active(self):
        """Not sold and not removed by the user (a removed one waits only to be deleted on the market)."""
        return self.status != self.Status.SOLD and not self.pending_delete

    @property
    def ct_product_id(self):
        """The CardTrader product id of a CardTrader listing (an int), None when not pushed or another market."""
        if self.market != Market.CARDTRADER or not self.market_listing_id.isdigit():
            return None
        return int(self.market_listing_id)

    @ct_product_id.setter
    def ct_product_id(self, value):
        self.market_listing_id = "" if value is None else str(value)


class MarketConnection(models.Model):
    """A user's state on one market: whether it is connected, their seller name there and the last sync.

    status: off (disconnected), never (connected, never synced), ok, error (the last sync failed, last_error). Both
    markets' rows are created (never) when the extension is paired and updated by its reports.
    """

    class Status(models.TextChoices):
        OK = "ok", "OK"
        ERROR = "error", "Error"
        NEVER = "never", "Never synced"
        OFF = "off", "Off"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="market_connections")
    market = models.CharField(max_length=2, choices=Market.choices)
    handle = models.CharField(max_length=120, blank=True, help_text="The seller name on the market")
    status = models.CharField(max_length=5, choices=Status.choices, default=Status.NEVER)
    last_sync = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    orders_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = owned_manager("user")

    class Meta:
        ordering = ["market"]
        constraints = [models.UniqueConstraint(fields=["user", "market"], name="unique_market_connection")]

    def __str__(self):
        return f"{self.user} on {self.get_market_display()}: {self.status}"

    @property
    def connected(self):
        return self.status != self.Status.OFF


class SyncConflict(models.Model):
    class Resolution(models.TextChoices):
        OPEN = "open", "Open"
        LEDGER = "ledger", "Kept Ledger"
        REMOTE = "remote", "Kept remote"

    market = models.CharField(max_length=2, choices=Market.choices, default=Market.CARDTRADER)
    item = models.ForeignKey(CollectionItem, on_delete=models.CASCADE, related_name="conflicts")
    field = models.CharField(max_length=40, blank=True, help_text="Listing field that differs, e.g. 'price'")
    local_value = models.CharField(max_length=120)
    remote_value = models.CharField(max_length=120)
    remote_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Numeric remote value (price or quantity) applied on 'Keep remote'",
    )
    resolution = models.CharField(max_length=8, choices=Resolution.choices, default=Resolution.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = owned_manager("item__owner")

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.item.card}: {self.local_value} ≠ {self.remote_value}"

    @property
    def listing(self):
        """The listing of the conflict's item on its market, or None."""
        return self.item.listing_for(self.market)


class SyncLog(models.Model):
    class Level(models.TextChoices):
        OK = "ok", "OK"
        WARNING = "warn", "Warning"
        ERROR = "error", "Error"  # a sync that failed (reported by the extension)

    owner = models.ForeignKey(  # None: a global entry (the catalog sync)
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True, related_name="sync_logs",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    level = models.CharField(max_length=5, choices=Level.choices, default=Level.OK)
    message = models.CharField(max_length=300)

    objects = SyncLogQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.message


class OrderQuerySet(OwnedQuerySet):
    owner_path = "owner"

    def with_stage(self):
        """Annotate stage, where the order is in the shop (see Order.Stage): the first that applies of issue (a note
        by the seller, or a problem state on the market), shipped (sent/arrived/done; also closed: merged into the
        CT Zero order that ships it, and canceled before payment: nothing to ship), packed, picking (some line
        picked), to_pack."""
        S = Order.State
        issue = (~Q(issue_note="") | Q(state__in=[S.REQUEST_FOR_CANCEL, S.LOST])
                 | Q(state=S.CANCELED, paid_at__isnull=False))
        picked = OrderItem.objects.filter(order=models.OuterRef("pk"), picked=True)
        return self.annotate(stage=models.Case(
            models.When(issue, then=models.Value(Order.Stage.ISSUE)),
            models.When(state__in=Order.DONE_STATES, then=models.Value(Order.Stage.SHIPPED)),
            models.When(packed_at__isnull=False, then=models.Value(Order.Stage.PACKED)),
            models.When(models.Exists(picked), then=models.Value(Order.Stage.PICKING)),
            default=models.Value(Order.Stage.TO_PACK),
            output_field=models.CharField(),
        ))


class Order(models.Model):
    """A sale order on a market (you are the seller)."""

    class Stage(models.TextChoices):
        """Where the order is in the shop (computed: OrderQuerySet.with_stage)."""

        TO_PACK = "to_pack", "To pack"
        PICKING = "picking", "Picking"
        PACKED = "packed", "Packed"
        SHIPPED = "shipped", "Shipped"
        ISSUE = "issue", "Issue"

    class State(models.TextChoices):
        HUB_PENDING = "hub_pending", "CT Zero pending"
        PAID = "paid", "To ship"
        SENT = "sent", "Shipped"
        ARRIVED = "arrived", "Arrived"
        DONE = "done", "Completed"
        REQUEST_FOR_CANCEL = "request_for_cancel", "Cancellation requested"
        CANCELED = "canceled", "Canceled"
        LOST = "lost", "Lost"
        CLOSED = "closed", "Merged (CT Zero)"

    # Left the shop (shipped) on the market
    SHIPPED_STATES = (State.SENT, State.ARRIVED, State.DONE)
    DELIVERED_STATES = (State.ARRIVED, State.DONE)
    # Nothing left to pack: shipped, merged into the CT Zero order, or canceled (an issue only when it was paid)
    DONE_STATES = (*SHIPPED_STATES, State.CLOSED, State.CANCELED)

    owner = _owner("orders")
    market = models.CharField(max_length=2, choices=Market.choices, default=Market.CARDTRADER)
    market_order_id = models.CharField(max_length=64, help_text="The order's id on the market")
    code = models.CharField(max_length=40)
    state = models.CharField(max_length=24, default=State.PAID)
    via_cardtrader_zero = models.BooleanField(default=False)
    buyer_username = models.CharField(max_length=120, blank=True)
    buyer_name = models.CharField(max_length=200, blank=True)
    buyer_email = models.CharField(max_length=200, blank=True)
    buyer_phone = models.CharField(max_length=40, blank=True)
    ship_name = models.CharField(max_length=200, blank=True)
    ship_street = models.CharField(max_length=300, blank=True)
    ship_zip = models.CharField(max_length=20, blank=True)
    ship_city = models.CharField(max_length=120, blank=True)
    ship_province = models.CharField(max_length=120, blank=True)
    ship_country = models.CharField(max_length=80, blank=True)
    ship_country_code = models.CharField(max_length=2, blank=True)
    ship_note = models.TextField(blank=True)
    shipping_method = models.CharField(max_length=200, blank=True)
    tracked = models.BooleanField(default=False)
    tracking_code = models.CharField(max_length=120, blank=True)
    max_shipping_days = models.PositiveIntegerField(null=True, blank=True)
    packing_number = models.PositiveIntegerField(null=True, blank=True)
    size = models.PositiveIntegerField(default=0)
    items_total = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    shipping_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    market_fee = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    total = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    net = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="total - market_fee")
    currency = models.CharField(max_length=3, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)
    stock_applied = models.BooleanField(
        default=False, help_text="Sold quantities already removed from the Ledger"
    )
    packed_at = models.DateTimeField(null=True, blank=True, help_text="When the seller packed it")
    issue_note = models.TextField(blank=True, help_text="A problem noted by the seller")
    issue_at = models.DateTimeField(null=True, blank=True)
    invoice_number = models.CharField(max_length=40, blank=True, help_text="Given when its packing slip is printed")
    raw = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OrderQuerySet.as_manager()

    class Meta:
        ordering = ["-paid_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["owner", "market", "market_order_id"],
                                               name="unique_order_market_id")]

    def __str__(self):
        return f"#{self.code}"

    @property
    def ct_id(self):
        """CardTrader's id of a CardTrader order (an int), None otherwise."""
        if self.market != Market.CARDTRADER or not self.market_order_id.isdigit():
            return None
        return int(self.market_order_id)

    @ct_id.setter
    def ct_id(self, value):
        self.market_order_id = str(value)

    @property
    def state_label(self):
        try:
            return self.State(self.state).label
        except ValueError:
            return self.state.replace("_", " ").capitalize()

    @property
    def can_ship(self):
        return self.state == self.State.PAID

    @property
    def address_lines(self):
        city = " ".join(p for p in (self.ship_zip, self.ship_city, self.ship_province) if p)
        return [l for l in (self.ship_name, self.ship_street, city, self.ship_country) if l]


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    market_item_id = models.CharField(max_length=64, blank=True, help_text="The order line's id on the market")
    market_listing_id = models.CharField(max_length=64, blank=True, help_text="The listing it sold, on the market")
    listing = models.ForeignKey(Listing, null=True, blank=True, on_delete=models.SET_NULL, related_name="order_items")
    name = models.CharField(max_length=200)
    expansion = models.CharField(max_length=200, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    condition = models.CharField(max_length=40, blank=True)
    language = models.CharField(max_length=10, blank=True)
    foil = models.BooleanField(default=False)
    picked = models.BooleanField(default=False, help_text="Taken from its storage location for packing")

    objects = owned_manager("order__owner")

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["order", "market_item_id"], condition=~Q(market_item_id=""),
                                               name="unique_order_item_market_id")]

    def __str__(self):
        return f"{self.quantity}× {self.name}"


class ShipmentQuerySet(OwnedQuerySet):
    owner_path = "order__owner"

    def with_status(self, today):
        """Annotate status (Shipment.Status) as of the date today: returned (marked by the seller), delivered
        (delivered_at, or arrived/done on the market), no_tracking (untracked method and no code), late (past
        expected_by), in_transit; and problem: late, or no_tracking past expected_by."""
        St = Shipment.Status
        return self.annotate(status=models.Case(
            models.When(returned_at__isnull=False, then=models.Value(St.RETURNED)),
            models.When(Q(delivered_at__isnull=False) | Q(order__state__in=Order.DELIVERED_STATES),
                        then=models.Value(St.DELIVERED)),
            models.When(order__tracked=False, tracking_code="", then=models.Value(St.NO_TRACKING)),
            models.When(expected_by__lt=today, then=models.Value(St.LATE)),
            default=models.Value(St.IN_TRANSIT),
            output_field=models.CharField(),
        )).annotate(problem=models.Case(
            models.When(Q(status=St.LATE) | Q(status=St.NO_TRACKING, expected_by__lt=today), then=True),
            default=False, output_field=models.BooleanField(),
        ))


class Shipment(models.Model):
    """The parcel of a shipped order (one per order), created when the order becomes sent: by an extension report
    or POST /api/extension/shipped/ (services.record_shipment). No tracking provider: its status comes from the dates,
    the market state and what the seller marks (status is computed: ShipmentQuerySet.with_status)."""

    class Status(models.TextChoices):
        IN_TRANSIT = "in_transit", "In transit"
        DELIVERED = "delivered", "Delivered"
        LATE = "late", "Late"
        NO_TRACKING = "no_tracking", "No tracking"
        RETURNED = "returned", "Returned"

    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="shipment")
    carrier = models.CharField(max_length=80, blank=True)
    tracking_code = models.CharField(max_length=120, blank=True)
    shipped_at = models.DateTimeField(null=True, blank=True)
    expected_by = models.DateField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    returned_at = models.DateTimeField(null=True, blank=True)
    postage_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    note = models.TextField(blank=True)

    objects = ShipmentQuerySet.as_manager()

    class Meta:
        ordering = ["-shipped_at", "-id"]

    def __str__(self):
        return f"Shipment of {self.order}"

    @property
    def tracked(self):
        return self.order.tracked or bool(self.tracking_code)


class Expansion(models.Model):
    """A set of a game (local catalog copy), e.g. an MTG set: its printings come from the generator files."""

    game = models.ForeignKey(Game, on_delete=models.PROTECT, related_name="expansions")
    code = models.CharField(max_length=40, help_text="Set code, lower case, unique per game")
    name = models.CharField(max_length=200)
    released_at = models.DateField(null=True, blank=True)
    language = models.CharField(
        max_length=5, blank=True, default="", help_text='Language of a set printed in one, e.g. "ja"',
    )

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["game", "code"], name="unique_expansion_game_code")]

    def __str__(self):
        return f"{self.code.upper()} · {self.name}"


class Category(models.Model):
    """A Cardmarket product category of a game (local catalog copy): single cards, boosters, displays, ..."""

    game = models.ForeignKey(Game, on_delete=models.PROTECT, related_name="categories")
    cm_id = models.PositiveIntegerField("Cardmarket category")
    name = models.CharField(max_length=100, blank=True)
    is_single = models.BooleanField(
        default=False, help_text="Single cards (collector number, condition, foil), e.g. \"Magic Single\"; "
                                 "every other category (sealed, accessories, ...) is a product",
    )

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "categories"
        constraints = [models.UniqueConstraint(fields=["game", "cm_id"], name="unique_category_game_cm_id")]

    def __str__(self):
        return self.name or f"Category {self.cm_id}"


class Blueprint(models.Model):
    """One printing of a card, or a product, in the local catalog: a Cardmarket product."""

    cm_product = models.OneToOneField(
        "CardmarketPrice", null=True, blank=True, on_delete=models.DO_NOTHING, db_constraint=False,
        related_name="blueprint",
        verbose_name="Cardmarket product", help_text="Cardmarket's idProduct; its price row may not exist",
    )
    expansion = models.ForeignKey(Expansion, on_delete=models.CASCADE, related_name="blueprints")
    name = models.CharField(max_length=200, db_index=True)
    collector_number = models.CharField(max_length=40, blank=True)
    rarity = models.CharField(max_length=40, blank=True)
    image_url = models.URLField(max_length=500, blank=True, help_text="Small image")
    image_large_url = models.URLField(max_length=500, blank=True, help_text="Normal image (488×680)")
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.SET_NULL, related_name="blueprints")

    class Meta:
        ordering = ["expansion__name", "name"]

    @property
    def is_product(self):
        """Sealed product or accessory rather than a card; a blueprint not yet categorised counts as a card."""
        return self.category_id is not None and not self.category.is_single

    def __str__(self):
        number = f" #{self.collector_number}" if self.collector_number else ""
        return f"{self.name} ({self.expansion.code.upper()}{number})"


class CardmarketPrice(models.Model):
    """Cardmarket price guide of one Cardmarket product (local copy of Cardmarket's public daily file)."""

    id_product = models.PositiveIntegerField("Cardmarket product", primary_key=True)
    low = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    trend = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    avg = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    avg1 = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    avg7 = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    avg30 = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    low_foil = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    trend_foil = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    avg_foil = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    avg1_foil = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    avg7_foil = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    avg30_foil = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    prices_date = models.DateTimeField(null=True, blank=True, help_text="createdAt of the price guide")

    def __str__(self):
        return f"Cardmarket {self.id_product}: {self.trend}"


class ScanSession(models.Model):
    """A camera scanning session: a draft of cards to add to the collection."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        SAVED = "saved", "Saved"

    owner = _owner("scan_sessions")
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    game = models.ForeignKey(Game, on_delete=models.PROTECT, related_name="scan_sessions")
    expansion = models.ForeignKey(Expansion, null=True, blank=True, on_delete=models.SET_NULL)
    default_condition = models.CharField(max_length=2, choices=Condition.choices, default=Condition.NEAR_MINT)
    default_language = models.CharField(max_length=3, choices=Language.choices, default=Language.EN)
    lot = models.ForeignKey("PurchaseLot", null=True, blank=True, on_delete=models.SET_NULL,
                            related_name="scan_sessions", help_text="The items saved from the session get this lot")
    status = models.CharField(max_length=5, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = owned_manager("owner")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Scan session {self.pk} ({self.expansion or 'all expansions'})"


class ScanLine(models.Model):
    """One draft row of a scan session: copies of a printing with the same language/foil/condition."""

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    session = models.ForeignKey(ScanSession, on_delete=models.CASCADE, related_name="lines")
    blueprint = models.ForeignKey(Blueprint, null=True, blank=True, on_delete=models.PROTECT)
    language = models.CharField(max_length=3, choices=Language.choices, blank=True)
    foil = models.BooleanField(default=False)
    condition = models.CharField(max_length=2, choices=Condition.choices)
    quantity = models.PositiveIntegerField(default=1)
    needs_confirmation = models.BooleanField(default=False)
    candidates = models.JSONField(default=list, blank=True, help_text="Cardmarket product ids to choose from")
    read_name = models.CharField(max_length=200, blank=True, help_text="The card name the model read")
    frame = models.BinaryField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = owned_manager("session__owner")

    class Meta:
        ordering = ["-updated_at", "-id"]

    def __str__(self):
        return f"{self.quantity}× {self.blueprint or 'unconfirmed card'}"


class ScanCapture(models.Model):
    """One photo sent by the phone, so a retried upload is never processed (or paid) twice."""

    session = models.ForeignKey(ScanSession, on_delete=models.CASCADE, related_name="captures")
    capture_id = models.CharField(max_length=64)
    line = models.ForeignKey(ScanLine, null=True, blank=True, on_delete=models.SET_NULL, related_name="captures")
    done = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["session", "capture_id"], name="unique_scan_capture")]

    def __str__(self):
        return f"{self.capture_id} → {self.line_id or '-'}"


class MissingCardReport(models.Model):
    """A user's report of a product missing from the catalog ("Report a missing card"), read in the admin."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="missing_card_reports")
    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="missing_card_reports")
    q = models.CharField("search", max_length=200)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.game}: {self.q}"


class ExtensionToken(models.Model):
    """A browser extension paired with a user's account: only the sha256 of its key is stored."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="extension_tokens")
    key = models.CharField(max_length=64, unique=True, help_text="sha256 hex of the token")
    name = models.CharField(max_length=100, blank=True, help_text='The browser it was paired from, e.g. "Chrome"')
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked = models.BooleanField(default=False)

    objects = owned_manager("user")

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.name or 'Extension'} of {self.user}"


class ExtensionPairing(models.Model):
    """A one-time pairing code shown on the Account page, claimed by the extension within its expiry."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="extension_pairings")
    code = models.CharField(max_length=9, unique=True, help_text='e.g. "ABCD-1234"')
    expires_at = models.DateTimeField()
    claimed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.code


class ExtensionRun(models.Model):
    """One sync run reported by the extension (POST /api/extension/report/), kept so a retried report is applied once."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="extension_runs")
    run_id = models.UUIDField()
    market = models.CharField(max_length=2, choices=Market.choices)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=300, blank=True)
    response = models.JSONField(default=dict, blank=True, help_text="The answer given, repeated on a retry")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = owned_manager("user")

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["user", "run_id"], name="unique_extension_run")]

    def __str__(self):
        return f"{self.market} run {self.run_id}"


class RepricingRule(models.Model):
    """A rule of the user's that sets listing prices in CardLedger (ledger/repricing.py evaluates them by position)."""

    class Kind(models.TextChoices):
        PRICE = "price", "Price"
        FLOOR = "floor", "Floor"
        OVER_TIME = "over_time", "Over time"

    class Markets(models.TextChoices):
        ALL = "all", "All markets"
        CARDMARKET = "cm", "Cardmarket"
        CARDTRADER = "ct", "CardTrader"

    class Reference(models.TextChoices):
        TREND = "trend", "Cardmarket trend"
        LOW = "low", "Cardmarket low"
        AVG7 = "avg7", "Cardmarket 7-day average"
        AVG30 = "avg30", "Cardmarket 30-day average"
        COST = "cost", "Cost"
        OTHER_MARKET = "other_market", "Price on the other market"
        CURRENT = "current", "Current price"

    class RoundTo(models.TextChoices):
        SELLER = "", "Seller setting"
        CENT = "0.01", "0.01"
        FIVE = "0.05", "0.05"
        TEN = "0.10", "0.10"
        FIFTY = "0.50", "0.50"
        ONE = "1.00", "1.00"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="repricing_rules")
    name = models.CharField(max_length=100)
    position = models.PositiveIntegerField(default=1, help_text="1-based order: the first matching price rule wins")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.PRICE)
    markets = models.CharField(max_length=3, choices=Markets.choices, default=Markets.ALL)
    conditions = models.JSONField(default=list, blank=True, help_text="[{field, op, value}]; empty: any listing")
    reference = models.CharField(max_length=12, choices=Reference.choices, default=Reference.TREND)
    adjust_pct = models.DecimalField(max_digits=7, decimal_places=2, default=0)
    adjust_abs = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    round_to = models.CharField(max_length=4, choices=RoundTo.choices, blank=True)
    interval_days = models.PositiveIntegerField(null=True, blank=True, help_text="Over time: one step every N days")
    enabled = models.BooleanField(default=True)
    draft = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = owned_manager("user")

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.name

    @property
    def active(self):
        return self.enabled and not self.draft


class RepricingRun(models.Model):
    """One application of the repricing rules (or of an ad-hoc reprice); the last 50 per user are kept."""

    class Trigger(models.TextChoices):
        MANUAL = "manual", "Run now"
        SYNC = "sync", "Before a sync"
        ADHOC = "adhoc", "Ad-hoc reprice"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="repricing_runs")
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    trigger = models.CharField(max_length=6, choices=Trigger.choices, default=Trigger.MANUAL)
    dry_run = models.BooleanField(default=False)
    changes = models.PositiveIntegerField(default=0)
    raised = models.PositiveIntegerField(default=0)
    lowered = models.PositiveIntegerField(default=0)
    value_delta = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                      help_text="Sum of (new - old price) × quantity")
    blocked = models.PositiveIntegerField(default=0, help_text="Listings a floor or the cost held up")

    objects = owned_manager("user")

    class Meta:
        ordering = ["-started_at", "-id"]

    def __str__(self):
        return f"{self.get_trigger_display()} {self.started_at:%Y-%m-%d %H:%M}"


class RepricingChange(models.Model):
    """One listing price a run changed; the listing's description is copied so the run stays readable."""

    run = models.ForeignKey(RepricingRun, on_delete=models.CASCADE, related_name="price_changes")
    listing = models.ForeignKey(Listing, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="repricing_changes")
    market = models.CharField(max_length=2, choices=Market.choices)
    old_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,
                                    help_text="None: the listing was created")
    new_price = models.DecimalField(max_digits=10, decimal_places=2)
    rule = models.ForeignKey(RepricingRule, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    floor_rule = models.ForeignKey(RepricingRule, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    blocked_by_floor = models.BooleanField(default=False)
    item_pk = models.PositiveIntegerField(null=True, blank=True, help_text="The collection item")
    name = models.CharField(max_length=200, blank=True)
    set_code = models.CharField(max_length=40, blank=True)
    condition = models.CharField(max_length=2, blank=True)
    language = models.CharField(max_length=3, blank=True)
    foil = models.BooleanField(default=False)
    rule_name = models.CharField(max_length=100, blank=True)
    floor_rule_name = models.CharField(max_length=100, blank=True)

    objects = owned_manager("run__user")

    class Meta:
        ordering = ["name", "market", "id"]

    def __str__(self):
        return f"{self.name} {self.market}: {self.old_price} → {self.new_price}"


class PurchaseLot(models.Model):
    """Cards bought in one go (a collection, a buylist, a restock, a trade-in), numbered PL-0001, PL-0002, ... per
    user; its items are the collection items whose lot it is (ledger/purchases.py computes its figures)."""

    class Type(models.TextChoices):
        COLLECTION = "collection", "Collection"
        BUYLIST = "buylist", "Buylist"
        RESTOCK = "restock", "Restock"
        TRADE_IN = "trade_in", "Trade-in"

    class PaidBy(models.TextChoices):
        CASH = "cash", "Cash"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        PAYPAL = "paypal", "PayPal"
        CARD = "card", "Card"
        STORE_CREDIT = "store_credit", "Store credit"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        IN_POST = "in_post", "In the post"
        RECEIVED = "received", "Received"
        GRADING = "grading", "Grading"
        LISTING = "listing", "Listing"
        DONE = "done", "Done"

    # Still to sort or grade (me's lots_to_process, the purchases' waiting KPIs and the "To grade" tab)
    TO_PROCESS = (Status.IN_POST, Status.RECEIVED, Status.GRADING)

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="purchase_lots")
    number = models.PositiveIntegerField(help_text="Per-user sequence (the code's number)")
    code = models.CharField(max_length=20, help_text='"PL-0001"')
    type = models.CharField(max_length=10, choices=Type.choices, default=Type.COLLECTION)
    seller = models.CharField(max_length=200, blank=True)
    where = models.CharField(max_length=200, blank=True, help_text="Where it was bought (a shop, a fair, online)")
    cards_estimate = models.PositiveIntegerField(null=True, blank=True, help_text="Cards expected, before sorting")
    price_paid = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    paid_by = models.CharField(max_length=13, choices=PaidBy.choices, default=PaidBy.CASH)
    bought_on = models.DateField()
    next_step = models.TextField(blank=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.IN_POST)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = owned_manager("user")

    class Meta:
        ordering = ["-bought_on", "-number"]
        constraints = [
            models.UniqueConstraint(fields=["user", "number"], name="unique_purchase_lot_number"),
            models.UniqueConstraint(fields=["user", "code"], name="unique_purchase_lot_code"),
        ]

    def __str__(self):
        return self.code


class PurchaseAttachment(models.Model):
    """A receipt or photo of a purchase lot (PDF or image) in the default storage, served under /media/."""

    lot = models.ForeignKey(PurchaseLot, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to="purchases/")
    name = models.CharField(max_length=200, help_text="The uploaded file's name")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    objects = owned_manager("lot__user")

    class Meta:
        ordering = ["uploaded_at", "id"]

    def __str__(self):
        return self.name


class PriceSnapshot(models.Model):
    """The Cardmarket trend of a product on a day it changed (written by the catalog price import; a product's value
    on any day is its latest snapshot on or before it). Rows older than KEEP_DAYS are pruned once a newer row also
    predates the cutoff."""

    KEEP_DAYS = 400

    cm_product_id = models.PositiveIntegerField("Cardmarket product")
    date = models.DateField()
    trend = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    trend_foil = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        ordering = ["cm_product_id", "date"]
        constraints = [models.UniqueConstraint(fields=["cm_product_id", "date"], name="unique_price_snapshot")]

    def __str__(self):
        return f"{self.cm_product_id} {self.date}: {self.trend}"


class StockValueSnapshot(models.Model):
    """The value (Cardmarket trend × copies) of a user's stock of a game on a day (ledger/snapshots.py)."""

    KEEP_DAYS = 400

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="stock_values")
    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="stock_values")
    date = models.DateField()
    value = models.DecimalField(max_digits=14, decimal_places=2)

    objects = owned_manager("user")

    class Meta:
        ordering = ["-date"]
        constraints = [models.UniqueConstraint(fields=["user", "game", "date"], name="unique_stock_value_snapshot")]

    def __str__(self):
        return f"{self.user} {self.game} {self.date}: {self.value}"


class SavedView(models.Model):
    """A collection filter the user saved (its query string, without page), shown as a tab."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_views")
    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="saved_views")
    name = models.CharField(max_length=60)
    query = models.CharField(max_length=500, blank=True, help_text='The collection query string, e.g. "view=bulk&set=DMU"')
    position = models.PositiveIntegerField(default=1, help_text="1-based order among the user's views of the game")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = owned_manager("user")

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.name


class Location(models.Model):
    """A storage location of the user's (a box, a binder page): its capacity in copies; the copies in it are the
    items whose location equals its code."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="locations")
    code = models.CharField(max_length=40, help_text='As written on the items, e.g. "A3·12"')
    capacity = models.PositiveIntegerField(null=True, blank=True, help_text="Copies it holds; empty: no limit")
    note = models.CharField(max_length=200, blank=True)

    objects = owned_manager("user")

    class Meta:
        ordering = ["code"]
        constraints = [models.UniqueConstraint(fields=["user", "code"], name="unique_location_code")]

    def __str__(self):
        return self.code
