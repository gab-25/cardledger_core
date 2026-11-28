"""The extension API (/api/extension/): pairing and tokens (session, from the Account page), and the endpoints the
browser extension calls with its token (header "Authorization: Token <token>", accepted only here)."""

from django.core.cache import cache
from django.shortcuts import get_object_or_404
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.exceptions import AuthenticationFailed, NotFound, Throttled, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from accounts.models import seller_settings_of

from .. import extension, repricer, services
from ..models import ExtensionToken, Market, RepricingRun
from .common import body, doc, messages_response, msg, query

TAGS = ["extension"]
CLAIM_LIMIT = 10  # claim attempts per IP address ...
CLAIM_WINDOW = 600  # ... in this many seconds


class ExtensionTokenAuthentication(BaseAuthentication):
    """'Authorization: Token <token>' with a live ExtensionToken; used only by the /api/extension/ endpoints."""

    keyword = "Token"

    def authenticate(self, request):
        auth = get_authorization_header(request).split()
        if not auth or auth[0].lower() != self.keyword.lower().encode():
            return None
        if len(auth) != 2:
            raise AuthenticationFailed("Invalid token header.")
        try:
            raw = auth[1].decode()
        except UnicodeError:
            raise AuthenticationFailed("Invalid token header.") from None
        found = extension.user_for_token(raw)
        if found is None:
            raise AuthenticationFailed("Invalid or revoked token.")
        return found

    def authenticate_header(self, request):
        return self.keyword


class ExtensionTokenScheme(OpenApiAuthenticationExtension):
    target_class = "ledger.api.extension.ExtensionTokenAuthentication"
    name = "extensionToken"

    def get_security_definition(self, auto_schema):
        return {"type": "apiKey", "in": "header", "name": "Authorization",
                "description": 'The extension\'s token: "Token <token>"'}


def token_view(methods):
    """An endpoint of the extension: its token only (no session)."""
    def wrap(view):
        view = permission_classes([IsAuthenticated])(view)
        view = authentication_classes([ExtensionTokenAuthentication])(view)
        return api_view(methods)(view)
    return wrap


def _user(user):
    return {"username": user.get_username(), "full_name": user.get_full_name()}


# --- Session: pairing and tokens --------------------------------------------

@doc(TAGS, "A pairing code for the extension (valid 10 minutes; earlier unclaimed codes stop working)")
@api_view(["POST"])
def pair(request):
    pairing = extension.start_pairing(request.user)
    return Response({"code": pairing.code, "expires_at": pairing.expires_at})


@doc(TAGS, f"The extension exchanges a pairing code for its token (no session; shown once). At most {CLAIM_LIMIT} "
           f"attempts per IP address every {CLAIM_WINDOW // 60} minutes (429 after)",
     request=body("ExtensionClaim", {"code": serializers.CharField(help_text='e.g. "ABCD-1234"'),
                                     "name": serializers.CharField(required=False, allow_blank=True,
                                                                   help_text="The browser, e.g. Chrome")}))
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def claim(request):
    key = f"extension-claim:{request.META.get('REMOTE_ADDR', '')}"
    cache.add(key, 0, CLAIM_WINDOW)
    try:
        attempts = cache.incr(key)
    except ValueError:  # expired in between
        cache.set(key, 1, CLAIM_WINDOW)
        attempts = 1
    if attempts > CLAIM_LIMIT:
        raise Throttled(wait=CLAIM_WINDOW, detail="Too many pairing attempts: try again later.")
    found = extension.claim(request.data.get("code"), request.data.get("name"))
    if found is None:
        raise ValidationError({"code": ["This pairing code is invalid or has expired: ask for a new one."]})
    raw, user = found
    return Response({"token": raw, "user": _user(user)})


def _token(t):
    return {"id": t.pk, "name": t.name, "created_at": t.created_at, "last_used_at": t.last_used_at,
            "revoked": t.revoked}


@doc(TAGS, "The extensions paired with the account (revoked ones are left out)")
@api_view(["GET"])
def tokens(request):
    return Response({"results": [_token(t) for t in ExtensionToken.objects.owned_by(request.user).filter(revoked=False)]})


@doc(TAGS, "A paired extension (GET) or revoke its token (DELETE)")
@extend_schema(methods=["GET"], operation_id="extension_token_retrieve")
@api_view(["GET", "DELETE"])
def token(request, pk):
    obj = get_object_or_404(ExtensionToken.objects.owned_by(request.user).filter(revoked=False), pk=pk)
    if request.method == "GET":
        return Response(_token(obj))
    obj.revoked = True
    obj.save(update_fields=["revoked"])
    return messages_response(msg("success", f"Extension “{obj.name or 'unnamed'}” disconnected."))


# --- Token: the extension's calls ------------------------------------------------

@doc(TAGS, "Extension: the user, the markets' state and the changes the next sync of each would push")
@token_view(["GET"])
def status(request):
    return Response({"user": _user(request.user), "markets": services.market_states(request.user),
                     "pending": extension.pending_counts(request.user)})


def _market_param(value):
    if value not in Market.values:
        raise ValidationError({"market": ["One of cm, ct."]})
    return value


@doc(TAGS, "Extension: the listings to create, update or delete on a market (with the seller's "
           "reprice_before_sync, the repricing rules run first on that market's listings)",
     parameters=[query("market", "cm: Cardmarket, ct: CardTrader", enum=Market.values)])
@token_view(["GET"])
def plan(request):
    market = _market_param(request.query_params.get("market", ""))
    if seller_settings_of(request.user).reprice_before_sync:
        repricer.run(request.user, trigger=RepricingRun.Trigger.SYNC, market=market)
    return Response(extension.plan(request.user, market))


class ResultSerializer(serializers.Serializer):
    listing_id = serializers.IntegerField()
    ok = serializers.BooleanField()
    market_listing_id = serializers.CharField(required=False, allow_blank=True, max_length=64)
    ct_blueprint_id = serializers.IntegerField(required=False, allow_null=True, min_value=1,
                                               help_text="CardTrader: the blueprint the listing was matched to "
                                                         "(stored on its card)")
    error = serializers.CharField(required=False, allow_blank=True)


class StockSerializer(serializers.Serializer):
    market_listing_id = serializers.CharField(max_length=64)
    cm_product_id = serializers.IntegerField(required=False, allow_null=True)
    ct_blueprint_id = serializers.IntegerField(required=False, allow_null=True, min_value=1,
                                               help_text="CardTrader: stored on the card of the known listing")
    name = serializers.CharField(allow_blank=True, default="")
    set_code = serializers.CharField(allow_blank=True, default="")
    collector_number = serializers.CharField(allow_blank=True, default="")
    condition = serializers.CharField(allow_blank=True, default="")
    language = serializers.CharField(allow_blank=True, default="")
    foil = serializers.BooleanField(default=False)
    quantity = serializers.IntegerField(min_value=0, required=False, allow_null=True)
    price = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True)


def _money_field():
    return serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True)


def _text(max_length):
    return serializers.CharField(required=False, allow_blank=True, max_length=max_length)


class OrderItemSerializer(serializers.Serializer):
    market_item_id = _text(64)
    market_listing_id = _text(64)
    name = serializers.CharField(max_length=200)
    expansion = _text(200)
    condition = _text(40)
    language = _text(10)
    foil = serializers.BooleanField(default=False)
    quantity = serializers.IntegerField(min_value=1, default=1)
    price = _money_field()


class OrderSerializer(serializers.Serializer):
    market_order_id = serializers.CharField(max_length=64)
    code = _text(40)
    state = serializers.ChoiceField(choices=["paid", "sent", "arrived", "canceled"])
    buyer_username = _text(120)
    buyer_name = _text(200)
    buyer_email = _text(200)
    buyer_phone = _text(40)
    ship_name = _text(200)
    ship_street = _text(300)
    ship_zip = _text(20)
    ship_city = _text(120)
    ship_province = _text(120)
    ship_country = _text(80)
    ship_country_code = _text(2)
    ship_note = serializers.CharField(required=False, allow_blank=True)
    shipping_method = _text(200)
    tracked = serializers.BooleanField(required=False)
    tracking_code = _text(120)
    paid_at = serializers.DateTimeField(required=False, allow_null=True)
    sent_at = serializers.DateTimeField(required=False, allow_null=True)
    items_total = _money_field()
    shipping_cost = _money_field()
    market_fee = _money_field()
    total = _money_field()
    net = _money_field()
    currency = _text(3)
    items = OrderItemSerializer(many=True, required=False)


class ReportSerializer(serializers.Serializer):
    run_id = serializers.UUIDField()
    market = serializers.ChoiceField(choices=Market.choices)
    started_at = serializers.DateTimeField(required=False, allow_null=True)
    finished_at = serializers.DateTimeField(required=False, allow_null=True)
    handle = serializers.CharField(required=False, allow_blank=True, max_length=120,
                                   help_text="The seller name on the market, when the extension read it")
    results = ResultSerializer(many=True, required=False)
    stock = StockSerializer(many=True, required=False, allow_null=True,
                            help_text="The user's whole stock on the market (absent: not read in this run)")
    orders = OrderSerializer(many=True, required=False, allow_null=True)
    error = serializers.CharField(required=False, allow_blank=True)


@doc(TAGS, "Extension: the results of one run on a market (idempotent per run_id: a retry answers the same). "
           "Applies the results to the listings, upserts the orders (sold copies removed once, the other markets' "
           "listings capped), compares the stock with the listings (conflicts) and answers {applied, conflicts, "
           "orders_new, unmatched}: unmatched are the stock rows not known here, with item_id a likely collection "
           "item or null", request=ReportSerializer)
@token_view(["POST"])
def report(request):
    serializer = ReportSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    return Response(extension.apply_report(request.user, serializer.validated_data))


@doc(TAGS, "Extension: an order was marked as shipped on its market, Cardmarket or CardTrader (the only way an order "
           "is shipped: the tracking code is saved, a paid or CT Zero pending order becomes sent; 404 when the user "
           "has no such order)",
     request=body("ExtensionShipped", {
         "market": serializers.ChoiceField(choices=Market.choices),
         "market_order_id": serializers.CharField(),
         "tracking_code": serializers.CharField(required=False, allow_blank=True),
     }))
@token_view(["POST"])
def shipped(request):
    data = request.data
    market = _market_param(data.get("market"))
    if not data.get("market_order_id"):
        raise ValidationError({"market_order_id": ["This field is required."]})
    order = extension.mark_shipped(request.user, market, data["market_order_id"],
                                   str(data.get("tracking_code") or "").strip())
    if order is None:
        raise NotFound()
    return Response({"id": order.pk, "state": order.state, "tracking_code": order.tracking_code})
