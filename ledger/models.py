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


class Order(models.Model):
    """A sale order on a market (you are the seller)."""

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
    raw = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = owned_manager("owner")

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

    objects = owned_manager("order__owner")

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["order", "market_item_id"], condition=~Q(market_item_id=""),
                                               name="unique_order_item_market_id")]

    def __str__(self):
        return f"{self.quantity}× {self.name}"


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
