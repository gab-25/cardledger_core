from django.conf import settings
from django.db import models


class Profile(models.Model):
    """A user's business details (shop name, VAT number), created on demand by profile_of()."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    shop_name = models.CharField(max_length=120, blank=True)
    vat_number = models.CharField("VAT number", max_length=40, blank=True)

    def __str__(self):
        return self.shop_name or f"Profile of {self.user}"


def profile_of(user, create=False):
    """The user's Profile; an unsaved blank one when they have none (create=True saves it)."""
    if create:
        return Profile.objects.get_or_create(user=user)[0]
    return Profile.objects.filter(user=user).first() or Profile(user=user)


class BusinessDetails(models.Model):
    """The seller's business details for packing slips and invoices (shop name and VAT number stay on Profile)."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="business")
    legal_name = models.CharField(max_length=200, blank=True)
    tax_code = models.CharField(max_length=40, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    buyer_email = models.EmailField("email for buyers", blank=True)
    website = models.URLField(max_length=300, blank=True)
    ship_street = models.CharField("ship-from street", max_length=300, blank=True)
    ship_city = models.CharField("ship-from city", max_length=120, blank=True)
    ship_postcode = models.CharField("ship-from postcode", max_length=20, blank=True)
    ship_country = models.CharField("ship-from country", max_length=80, blank=True)
    invoice_prefix = models.CharField(max_length=20, blank=True)
    invoice_next_number = models.PositiveIntegerField(default=1)
    invoice_footer = models.TextField(blank=True)
    logo = models.FileField(upload_to="logos/", blank=True)

    class Meta:
        verbose_name_plural = "business details"

    def __str__(self):
        return self.legal_name or f"Business of {self.user}"


def business_of(user):
    """The user's BusinessDetails; an unsaved blank one when they have none."""
    return BusinessDetails.objects.filter(user=user).first() or BusinessDetails(user=user)


class SellerSettings(models.Model):
    """How CardLedger sells for the user."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="seller_settings")
    auto_import_orders = models.BooleanField(default=True)
    push_prices = models.BooleanField(default=True)
    hold_below_cost = models.BooleanField(
        default=False, help_text="Bulk list/reprice skip the items whose new price would be under their cost",
    )
    default_condition = models.CharField(max_length=2, default="NM", help_text="Condition proposed when adding")
    price_rounding = models.CharField(
        max_length=10, blank=True, help_text='Step bulk prices are rounded to, e.g. "0.05"; empty: the default 0.05',
    )
    reprice_before_sync = models.BooleanField(
        default=False, help_text="Run the repricing rules right before each extension sync (its plan)",
    )

    class Meta:
        verbose_name_plural = "seller settings"

    def __str__(self):
        return f"Seller settings of {self.user}"


def seller_settings_of(user):
    """The user's SellerSettings; an unsaved one with the defaults when they have none."""
    return SellerSettings.objects.filter(user=user).first() or SellerSettings(user=user)


class AuditEvent(models.Model):
    """A security event of a user's account: sign-ins and password changes (TOKEN_* only on events of earlier versions,
    when a CardTrader token was stored)."""

    class Kind(models.TextChoices):
        SIGN_IN = "sign_in", "Signed in"
        SIGN_IN_FAILED = "sign_in_failed", "Failed sign-in"
        PASSWORD_CHANGED = "password_changed", "Password changed"
        PASSWORD_RESET = "password_reset", "Password reset"
        TOKEN_SAVED = "token_saved", "CardTrader token saved"
        TOKEN_REMOVED = "token_removed", "CardTrader token removed"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="audit_events")
    at = models.DateTimeField(auto_now_add=True)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    text = models.CharField(max_length=200)
    ip = models.GenericIPAddressField(null=True, blank=True)
    device = models.CharField(max_length=200, blank=True, help_text="The browser's User-Agent")

    class Meta:
        ordering = ["-at", "-id"]

    def __str__(self):
        return f"{self.user}: {self.text}"


def record(user, kind, request=None, text=""):
    """Add an AuditEvent of kind for user, with the IP address and User-Agent of request."""
    meta = request.META if request is not None else {}
    return AuditEvent.objects.create(
        user=user, kind=kind, text=(text or AuditEvent.Kind(kind).label)[:200],
        ip=meta.get("REMOTE_ADDR") or None, device=meta.get("HTTP_USER_AGENT", "")[:200],
    )
