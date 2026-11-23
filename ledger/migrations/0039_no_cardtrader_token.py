"""No CardTrader token anymore: CardTrader is synced by the browser extension like Cardmarket. SyncService (the
stored tokens, dropped with it) goes; its sync times are kept on the user's CardTrader MarketConnection. Game loses
ct_game_id (CardTrader's game id, used only to call CardTrader's API)."""

from django.db import migrations, models

CARDTRADER = "CardTrader"


def forwards(apps, schema_editor):
    SyncService = apps.get_model("ledger", "SyncService")
    MarketConnection = apps.get_model("ledger", "MarketConnection")
    for service in SyncService.objects.filter(name=CARDTRADER):
        if service.last_sync is None and service.orders_synced_at is None:
            continue
        conn = MarketConnection.objects.filter(user_id=service.user_id, market="ct").first()
        if conn is None:
            continue  # never connected: CardTrader stays off until the extension is paired
        if conn.last_sync is None or service.last_sync and service.last_sync > conn.last_sync:
            conn.last_sync = service.last_sync
        if conn.orders_synced_at is None or service.orders_synced_at and service.orders_synced_at > conn.orders_synced_at:
            conn.orders_synced_at = service.orders_synced_at
        conn.save(update_fields=["last_sync", "orders_synced_at"])


class Migration(migrations.Migration):

    dependencies = [
        ("ledger", "0038_markets_cleanup"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="syncconflict",
            name="service",
        ),
        migrations.DeleteModel(
            name="SyncService",
        ),
        migrations.RemoveField(
            model_name="game",
            name="ct_game_id",
        ),
        migrations.AlterField(
            model_name="card",
            name="ct_blueprint_id",
            field=models.PositiveIntegerField(
                blank=True, help_text="Reported by the extension's CardTrader sync (results or stock of a known listing)",
                null=True, verbose_name="CardTrader blueprint"),
        ),
    ]
