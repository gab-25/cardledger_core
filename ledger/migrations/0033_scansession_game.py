# A scan session belongs to a game: the sessions there are already are Magic ones.

import django.db.models.deletion
from django.db import migrations, models


def sessions_are_magic(apps, schema_editor):
    ScanSession = apps.get_model("ledger", "ScanSession")
    if not ScanSession.objects.filter(game__isnull=True).exists():
        return
    magic = apps.get_model("ledger", "Game").objects.get(slug="mtg")
    ScanSession.objects.filter(game__isnull=True).update(game=magic)


class Migration(migrations.Migration):

    dependencies = [
        ('ledger', '0032_longer_set_codes'),
    ]

    operations = [
        migrations.AddField(
            model_name='scansession',
            name='game',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT,
                                    related_name='scan_sessions', to='ledger.game'),
        ),
        migrations.RunPython(sessions_are_magic, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='scansession',
            name='game',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                    related_name='scan_sessions', to='ledger.game'),
        ),
    ]
