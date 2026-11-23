"""Daily history: Cardmarket trends of the catalog (PriceSnapshot) and the value of each user's stock (StockValueSnapshot).

- Price snapshots are written by the catalog price import (catalog_sync), per game, right after its prices: a row for
  each product whose trend or foil trend differs from its latest snapshot, dated the price guide's local date (a rerun
  the same day overwrites that day's row). A product's trend on a day is its latest snapshot on or before that day.
  Rows older than PriceSnapshot.KEEP_DAYS go once a newer row of the product also predates the cutoff, so the value
  at the cutoff is always known.
- Stock values are written for every user and game they have items of, once a day: by the management command
  `snapshot_stock_values` (schedule it daily, e.g. cron after the nightly `sync_catalog`) and at the end of every
  catalog sync. A rerun the same day overwrites the day's row.
"""

from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.db.models import DecimalField, ExpressionWrapper, F, Sum
from django.utils import timezone

from .cardmarket import trend_price
from .models import CardmarketPrice, CollectionItem, PriceSnapshot, StockValueSnapshot

WEEK_AGO_DAYS = 7
WEEK_AGO_WINDOW = 7  # a week-ago value may be up to 7 more days older (a missed day)


def snapshot_prices(game, day):
    """Snapshot the trends of game's products that changed since their latest snapshot, dated day. Returns rows
    written."""
    snap, price = PriceSnapshot._meta.db_table, CardmarketPrice._meta.db_table
    from .models import Blueprint, Expansion

    blueprint, expansion = Blueprint._meta.db_table, Expansion._meta.db_table
    sql = f"""
        INSERT INTO {snap} (cm_product_id, date, trend, trend_foil)
        SELECT p.id_product, %(day)s, p.trend, p.trend_foil
        FROM {price} p
        JOIN {blueprint} b ON b.cm_product_id = p.id_product
        JOIN {expansion} e ON e.id = b.expansion_id
        WHERE e.game_id = %(game)s AND NOT EXISTS (
            SELECT 1 FROM (
                SELECT s.trend, s.trend_foil FROM {snap} s
                WHERE s.cm_product_id = p.id_product AND s.date <= %(day)s ORDER BY s.date DESC LIMIT 1
            ) last
            WHERE last.trend IS NOT DISTINCT FROM p.trend AND last.trend_foil IS NOT DISTINCT FROM p.trend_foil
        )
        ON CONFLICT (cm_product_id, date) DO UPDATE SET trend = EXCLUDED.trend, trend_foil = EXCLUDED.trend_foil
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, {"day": day, "game": game.pk})
        written = cursor.rowcount
    prune_prices(day)
    return written


def prune_prices(today):
    """Drop the snapshots older than KEEP_DAYS whose product has a newer one also before the cutoff."""
    cutoff = today - timedelta(days=PriceSnapshot.KEEP_DAYS)
    snap = PriceSnapshot._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(f"""
            DELETE FROM {snap} s WHERE s.date < %(cutoff)s AND EXISTS (
                SELECT 1 FROM {snap} n WHERE n.cm_product_id = s.cm_product_id AND n.date > s.date
                AND n.date <= %(cutoff)s)
        """, {"cutoff": cutoff})


def trends_on(products, day):
    """{product: (trend, trend_foil)} as of day (the latest snapshot on or before it)."""
    rows = (PriceSnapshot.objects.filter(cm_product_id__in=products, date__lte=day)
            .order_by("cm_product_id", "-date").distinct("cm_product_id")
            .values_list("cm_product_id", "trend", "trend_foil"))
    return {p: (t, tf) for p, t, tf in rows}


def change_pct(now, then):
    """Signed % change from then to now, one decimal ("+12.5"), None when either is unknown or then is 0."""
    if not now or not then:
        return None
    return f"{(now - then) / then * 100:+.1f}"


VALUE = ExpressionWrapper(F("trend") * F("quantity"), output_field=DecimalField(max_digits=20, decimal_places=6))


def snapshot_stock_values(day=None):
    """Write today's (or day's) stock value of every user and game they have items of. Returns rows written."""
    day = day or timezone.localdate()
    rows = (CollectionItem.objects.annotate(trend=trend_price()).values("owner_id", "card__game_id")
            .annotate(value=Sum(VALUE)).order_by())
    snapshots = [StockValueSnapshot(user_id=r["owner_id"], game_id=r["card__game_id"], date=day,
                                    value=(r["value"] or Decimal("0")).quantize(Decimal("0.01")))
                 for r in rows]
    StockValueSnapshot.objects.bulk_create(snapshots, update_conflicts=True,
                                           unique_fields=["user", "game", "date"], update_fields=["value"])
    StockValueSnapshot.objects.filter(date__lt=day - timedelta(days=StockValueSnapshot.KEEP_DAYS)).delete()
    return len(snapshots)


def value_week_ago(user, game, today=None):
    """The user's stock value of game a week ago: the latest snapshot dated 7 to 13 days ago, else None."""
    today = today or timezone.localdate()
    day = today - timedelta(days=WEEK_AGO_DAYS)
    snap = (StockValueSnapshot.objects.owned_by(user)
            .filter(game=game, date__lte=day, date__gt=day - timedelta(days=WEEK_AGO_WINDOW))
            .order_by("-date").first())
    return None if snap is None else snap.value
