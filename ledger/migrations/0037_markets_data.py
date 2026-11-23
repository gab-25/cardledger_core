"""Everything there is so far is CardTrader's: listings, orders and conflicts become market "ct", their CardTrader
ids are copied into the market ids, and each user with a saved CardTrader token gets a CardTrader connection."""

from django.db import migrations
from django.db.models import F

CARDTRADER = "CardTrader"


def forwards(apps, schema_editor):
    Listing = apps.get_model("ledger", "Listing")
    Order = apps.get_model("ledger", "Order")
    OrderItem = apps.get_model("ledger", "OrderItem")
    SyncConflict = apps.get_model("ledger", "SyncConflict")
    SyncService = apps.get_model("ledger", "SyncService")
    SyncLog = apps.get_model("ledger", "SyncLog")
    MarketConnection = apps.get_model("ledger", "MarketConnection")

    Listing.objects.update(market="ct")
    for listing in Listing.objects.filter(ct_product_id__isnull=False).iterator():
        listing.market_listing_id = str(listing.ct_product_id)
        listing.save(update_fields=["market_listing_id"])
    # The first publish time is unknown: the last change of a listing that reached CardTrader is the best guess.
    Listing.objects.filter(ct_product_id__isnull=False, listed_at__isnull=True).update(listed_at=F("updated_at"))

    Order.objects.update(market="ct")
    for order in Order.objects.iterator():
        order.market_order_id = str(order.ct_id)
        order.save(update_fields=["market_order_id"])
    for oi in OrderItem.objects.iterator():
        oi.market_item_id = str(oi.ct_id)
        oi.market_listing_id = "" if oi.ct_product_id is None else str(oi.ct_product_id)
        oi.save(update_fields=["market_item_id", "market_listing_id"])

    SyncConflict.objects.update(market="ct")

    for service in SyncService.objects.filter(name=CARDTRADER).exclude(api_token=""):
        latest = (SyncLog.objects.filter(owner_id=service.user_id, message__startswith=f"{CARDTRADER}:")
                  .order_by("-created_at", "-id").first())
        if service.last_sync is None:
            status = "never"
        elif latest is not None and latest.level == "error":
            status = "error"
        else:
            status = "ok"
        MarketConnection.objects.update_or_create(user_id=service.user_id, market="ct", defaults={
            "status": status, "last_sync": service.last_sync, "orders_synced_at": service.orders_synced_at,
            "last_error": latest.message[:300] if status == "error" else "",
        })


def backwards(apps, schema_editor):
    Listing = apps.get_model("ledger", "Listing")
    Order = apps.get_model("ledger", "Order")
    OrderItem = apps.get_model("ledger", "OrderItem")
    # Only CardTrader rows existed before: the other markets' ones can't go back.
    Listing.objects.exclude(market="ct").delete()
    Order.objects.exclude(market="ct").delete()
    for listing in Listing.objects.exclude(market_listing_id="").iterator():
        listing.ct_product_id = int(listing.market_listing_id) if listing.market_listing_id.isdigit() else None
        listing.save(update_fields=["ct_product_id"])
    for order in Order.objects.iterator():
        order.ct_id = int(order.market_order_id) if order.market_order_id.isdigit() else 0
        order.save(update_fields=["ct_id"])
    for oi in OrderItem.objects.iterator():
        oi.ct_id = int(oi.market_item_id) if oi.market_item_id.isdigit() else 0
        oi.ct_product_id = int(oi.market_listing_id) if oi.market_listing_id.isdigit() else None
        oi.save(update_fields=["ct_id", "ct_product_id"])


class Migration(migrations.Migration):

    dependencies = [
        ("ledger", "0036_markets_extension"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
