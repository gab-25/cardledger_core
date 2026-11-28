"""Shipping dates and carriers: pure helpers (no database), also used by the Shipment backfill migration.

- Shipping deadline of an order (due_by): the local date (Europe/Rome) of paid_at plus max_shipping_days working days
  (Monday-Friday; holidays are not known), DEFAULT_SHIPPING_DAYS when the market gives none.
- Expected delivery of a shipment (expected_by): the local date of shipped_at plus max_shipping_days calendar days,
  or TRACKED_DAYS / UNTRACKED_DAYS when the order has none.
"""

import re
from datetime import timedelta

from django.utils import timezone

DEFAULT_SHIPPING_DAYS = 2  # working days to ship when the order doesn't say
TRACKED_DAYS = 5  # calendar days to deliver a tracked parcel when the order doesn't say
UNTRACKED_DAYS = 10

# Lower-case words (whole words) of a shipping method → the carrier (first match wins)
CARRIERS = [
    ("dhl", "DHL"), ("ups", "UPS"), ("fedex", "FedEx"), ("gls", "GLS"), ("dpd", "DPD"), ("tnt", "TNT"),
    ("brt", "BRT"), ("sda", "SDA"), ("hermes", "Hermes"), ("evri", "Evri"), ("postnl", "PostNL"),
    ("royal mail", "Royal Mail"), ("colissimo", "La Poste"), ("la poste", "La Poste"), ("lettre", "La Poste"),
    ("deutsche post", "Deutsche Post"), ("warenpost", "Deutsche Post"), ("einschreiben", "Deutsche Post"),
    ("correos", "Correos"), ("poste italiane", "Poste Italiane"), ("raccomandata", "Poste Italiane"),
    ("posta1", "Poste Italiane"), ("bpost", "bpost"), ("inpost", "InPost"),
]


def local_date(moment):
    return timezone.localtime(moment).date() if moment else None


def add_working_days(day, n):
    """day plus n working days (Monday-Friday)."""
    while n > 0:
        day += timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def due_by(paid_at, max_shipping_days):
    """The date an order must be shipped by, None without paid_at."""
    if paid_at is None:
        return None
    days = DEFAULT_SHIPPING_DAYS if max_shipping_days is None else max_shipping_days
    return add_working_days(local_date(paid_at), days)


def due(deadline, today):
    """overdue (past the deadline), today (the deadline is today), ok; None without a deadline."""
    if deadline is None:
        return None
    if today > deadline:
        return "overdue"
    return "today" if today == deadline else "ok"


def expected_by(shipped_at, max_shipping_days, tracked):
    """The date a parcel shipped at shipped_at should be delivered by, None without shipped_at."""
    if shipped_at is None:
        return None
    if max_shipping_days is None:
        max_shipping_days = TRACKED_DAYS if tracked else UNTRACKED_DAYS
    return local_date(shipped_at) + timedelta(days=max_shipping_days)


def carrier_for(shipping_method):
    """The carrier a shipping method names ("" when not obvious)."""
    method = (shipping_method or "").lower()
    for word, carrier in CARRIERS:
        if re.search(rf"\b{re.escape(word)}\b", method):
            return carrier
    return ""
