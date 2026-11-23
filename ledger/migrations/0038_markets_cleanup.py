from django.db import migrations, models


class Migration(migrations.Migration):
    """Drop the CardTrader-only ids (copied into the market ids by 0037) and add the per-market uniqueness."""

    dependencies = [
        ('ledger', '0037_markets_data'),
    ]

    operations = [
        migrations.AlterField(
            model_name='order',
            name='market_order_id',
            field=models.CharField(help_text="The order's id on the market", max_length=64),
        ),
        migrations.RemoveConstraint(
            model_name='orderitem',
            name='unique_order_item',
        ),
        migrations.RemoveField(
            model_name='listing',
            name='ct_product_id',
        ),
        migrations.RemoveField(
            model_name='order',
            name='ct_id',
        ),
        migrations.AddConstraint(
            model_name='listing',
            constraint=models.UniqueConstraint(fields=('item', 'market'), name='unique_listing_item_market'),
        ),
        migrations.AddConstraint(
            model_name='order',
            constraint=models.UniqueConstraint(fields=('owner', 'market', 'market_order_id'), name='unique_order_market_id'),
        ),
        migrations.AddConstraint(
            model_name='orderitem',
            constraint=models.UniqueConstraint(condition=models.Q(('market_item_id', ''), _negated=True), fields=('order', 'market_item_id'), name='unique_order_item_market_id'),
        ),
        migrations.RemoveField(
            model_name='orderitem',
            name='ct_id',
        ),
        migrations.RemoveField(
            model_name='orderitem',
            name='ct_product_id',
        ),
    ]
