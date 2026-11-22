"""Games: the catalog (expansions, categories) and the cards each belong to one; existing rows are Magic."""

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q

GAMES = [  # slug, name, Cardmarket idGame, CardTrader game_id
    ("mtg", "Magic: The Gathering", 1, 1),
    ("pokemon", "Pokémon", 6, None),
    ("lorcana", "Disney Lorcana", 19, None),
    ("onepiece", "One Piece", 18, None),
    ("yugioh", "Yu-Gi-Oh!", 3, None),
    ("riftbound", "Riftbound", 22, None),
]
MAGIC_SINGLE = 1  # Cardmarket's "Magic Single" category


def create_games(apps, schema_editor):
    Game = apps.get_model("ledger", "Game")
    for slug, name, cm_id, ct_game_id in GAMES:
        Game.objects.update_or_create(slug=slug, defaults={"name": name, "cm_id": cm_id, "ct_game_id": ct_game_id})
    magic = Game.objects.get(slug="mtg")
    for model in ("Expansion", "Category", "Card"):
        apps.get_model("ledger", model).objects.filter(game__isnull=True).update(game=magic)
    apps.get_model("ledger", "Category").objects.filter(cm_id=MAGIC_SINGLE).update(is_single=True)
    # The rows just written must be checked now: PostgreSQL refuses to alter a table with pending trigger events.
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def game_field(related_name, null):
    return models.ForeignKey(
        null=null, on_delete=django.db.models.deletion.PROTECT, related_name=related_name, to="ledger.game",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0027_catalog_cardmarket_schema"),
    ]

    operations = [
        migrations.CreateModel(
            name="Game",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slug", models.SlugField(unique=True)),
                ("name", models.CharField(max_length=100)),
                ("cm_id", models.PositiveIntegerField(help_text="Cardmarket's idGame", unique=True,
                                                      verbose_name="Cardmarket game")),
                ("ct_game_id", models.PositiveIntegerField(blank=True, null=True, verbose_name="CardTrader game")),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.AddField(model_name="expansion", name="game", field=game_field("expansions", True)),
        migrations.AddField(model_name="category", name="game", field=game_field("categories", True)),
        migrations.AddField(model_name="card", name="game", field=game_field("cards", True)),
        migrations.AddField(
            model_name="category",
            name="is_single",
            field=models.BooleanField(
                default=False,
                help_text='Single cards (collector number, condition, foil), e.g. "Magic Single"; '
                          "every other category (sealed, accessories, ...) is a product",
            ),
        ),
        migrations.AddField(
            model_name="expansion",
            name="language",
            field=models.CharField(blank=True, default="", help_text='Language of a set printed in one, e.g. "ja"',
                                   max_length=5),
        ),
        migrations.RunPython(create_games, migrations.RunPython.noop),
        migrations.AlterField(model_name="expansion", name="game", field=game_field("expansions", False)),
        migrations.AlterField(model_name="category", name="game", field=game_field("categories", False)),
        migrations.AlterField(model_name="card", name="game", field=game_field("cards", False)),
        migrations.RemoveConstraint(model_name="card", name="unique_card_set_number"),
        migrations.RemoveConstraint(model_name="card", name="unique_card_legacy_name_set"),
        migrations.AddConstraint(
            model_name="card",
            constraint=models.UniqueConstraint(
                condition=~Q(collector_number=""), fields=("game", "set_code", "collector_number"),
                name="unique_card_set_number",
            ),
        ),
        migrations.AddConstraint(
            model_name="card",
            constraint=models.UniqueConstraint(
                condition=Q(collector_number=""), fields=("game", "name", "set_code"),
                name="unique_card_legacy_name_set",
            ),
        ),
        migrations.AlterField(
            model_name="expansion",
            name="code",
            field=models.CharField(help_text="Set code, lower case (Scryfall's for Magic), unique per game",
                                   max_length=10),
        ),
        migrations.AddConstraint(
            model_name="expansion",
            constraint=models.UniqueConstraint(fields=("game", "code"), name="unique_expansion_game_code"),
        ),
        migrations.AlterField(
            model_name="category",
            name="cm_id",
            field=models.PositiveIntegerField(verbose_name="Cardmarket category"),
        ),
        migrations.AddConstraint(
            model_name="category",
            constraint=models.UniqueConstraint(fields=("game", "cm_id"), name="unique_category_game_cm_id"),
        ),
    ]
