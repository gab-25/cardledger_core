"""A Shipment for every order already shipped (sent, arrived or done on its market), as services.record_shipment
would create it: shipped at sent_at (else when the order was last updated), its tracking code, the carrier its
shipping method names, expected_by from ledger.shipping; an arrived/done order counts as delivered when it was last
updated."""

from django.db import migrations

SHIPPED = ("sent", "arrived", "done")
DELIVERED = ("arrived", "done")


def forwards(apps, schema_editor):
    from ledger.shipping import carrier_for, expected_by

    Order = apps.get_model("ledger", "Order")
    Shipment = apps.get_model("ledger", "Shipment")
    rows = []
    for order in Order.objects.filter(state__in=SHIPPED, shipment__isnull=True).iterator():
        shipped_at = order.sent_at or order.updated_at
        rows.append(Shipment(
            order=order, carrier=carrier_for(order.shipping_method), tracking_code=order.tracking_code,
            shipped_at=shipped_at,
            expected_by=expected_by(shipped_at, order.max_shipping_days, order.tracked or bool(order.tracking_code)),
            delivered_at=order.updated_at if order.state in DELIVERED else None,
        ))
    Shipment.objects.bulk_create(rows, batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ("ledger", "0042_orders_packing_shipments"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
