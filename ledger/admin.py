from django.contrib import admin

from .models import (Blueprint, Card, CardmarketPrice, Category, CollectionItem, Expansion, Game, Listing, Location,
                     MarketConnection, ExtensionToken, MissingCardReport, Order, OrderItem, PurchaseAttachment,
                     PurchaseLot, Shipment, SyncConflict, SyncLog)


@admin.register(Card)
class CardAdmin(admin.ModelAdmin):
    list_display = ["name", "game", "set_code", "collector_number", "ct_blueprint_id"]
    list_filter = ["game", "set_code"]
    search_fields = ["name"]


@admin.register(CollectionItem)
class CollectionItemAdmin(admin.ModelAdmin):
    list_display = ["card", "owner", "condition", "language", "foil", "quantity"]
    list_filter = ["owner", "condition", "language", "foil"]
    search_fields = ["card__name"]
    autocomplete_fields = ["card"]
    raw_id_fields = ["owner"]


@admin.register(Listing)
class ListingAdmin(admin.ModelAdmin):
    list_display = ["item", "market", "quantity", "price", "status", "market_listing_id", "pending_delete"]
    list_filter = ["market", "status"]


@admin.register(SyncConflict)
class SyncConflictAdmin(admin.ModelAdmin):
    list_display = ["item", "market", "field", "local_value", "remote_value", "resolution"]
    list_filter = ["resolution", "market"]


@admin.register(SyncLog)
class SyncLogAdmin(admin.ModelAdmin):
    list_display = ["created_at", "owner", "level", "message"]
    list_filter = ["level", "owner"]
    raw_id_fields = ["owner"]


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    raw_id_fields = ["listing"]


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ["code", "market", "owner", "state", "buyer_username", "ship_country_code", "size", "total", "paid_at", "sent_at"]
    list_filter = ["market", "owner", "state", "via_cardtrader_zero"]
    raw_id_fields = ["owner"]
    search_fields = ["code", "buyer_username", "ship_name"]
    inlines = [OrderItemInline]
    readonly_fields = ["raw"]


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = ["order", "carrier", "tracking_code", "shipped_at", "expected_by", "delivered_at", "returned_at"]
    list_filter = ["order__market", "carrier"]
    raw_id_fields = ["order"]
    search_fields = ["order__code", "tracking_code"]


@admin.register(Game)
class GameAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "cm_id"]


@admin.register(Expansion)
class ExpansionAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "game", "language", "released_at"]
    list_filter = ["game"]
    search_fields = ["code", "name"]


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "game", "cm_id", "is_single"]
    list_filter = ["game"]
    search_fields = ["name"]


@admin.register(Blueprint)
class BlueprintAdmin(admin.ModelAdmin):
    list_display = ["name", "expansion", "category", "collector_number", "rarity", "cm_product_id"]
    list_filter = ["category"]
    search_fields = ["name", "expansion__code"]
    list_select_related = ["expansion", "category"]


@admin.register(CardmarketPrice)
class CardmarketPriceAdmin(admin.ModelAdmin):
    list_display = ["id_product", "trend", "trend_foil", "low", "avg", "prices_date"]
    search_fields = ["id_product"]


@admin.register(MissingCardReport)
class MissingCardReportAdmin(admin.ModelAdmin):
    list_display = ["q", "game", "user", "created_at"]
    list_filter = ["game"]
    search_fields = ["q", "note"]
    readonly_fields = ["created_at"]


@admin.register(MarketConnection)
class MarketConnectionAdmin(admin.ModelAdmin):
    list_display = ["user", "market", "handle", "status", "last_sync"]
    list_filter = ["market", "status"]
    raw_id_fields = ["user"]


@admin.register(ExtensionToken)
class ExtensionTokenAdmin(admin.ModelAdmin):
    list_display = ["user", "name", "created_at", "last_used_at", "revoked"]
    list_filter = ["revoked"]
    raw_id_fields = ["user"]
    readonly_fields = ["key"]


class PurchaseAttachmentInline(admin.TabularInline):
    model = PurchaseAttachment
    extra = 0


@admin.register(PurchaseLot)
class PurchaseLotAdmin(admin.ModelAdmin):
    list_display = ["code", "user", "type", "seller", "price_paid", "bought_on", "status"]
    list_filter = ["type", "status"]
    search_fields = ["code", "seller"]
    raw_id_fields = ["user"]
    inlines = [PurchaseAttachmentInline]


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ["code", "user", "capacity", "note"]
    raw_id_fields = ["user"]
