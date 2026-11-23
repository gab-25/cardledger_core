from decimal import Decimal

from django import forms
from django.db.models import F
from django.db.models.functions import Now

from accounts.models import seller_settings_of

from . import catalog
from .models import Blueprint, Card, CollectionItem, Condition, Language, Listing, Market


def get_or_create_card(game, name, set_code, collector_number="", **defaults):
    """The game's printing (set + collector number) when the number is known, else the legacy name + set row."""
    if collector_number:
        card, _ = Card.objects.get_or_create(
            game=game, set_code=set_code, collector_number=collector_number, defaults={"name": name, **defaults}
        )
    else:
        card, _ = Card.objects.get_or_create(
            game=game, name=name, set_code=set_code, collector_number="", defaults=defaults
        )
    return card


PRODUCT_CONDITION = Condition.NEAR_MINT  # stored on products, which have no condition


class CatalogItemForm(forms.Form):
    """Adds copies of a catalog product (card, sealed or accessory) to the owner's collection."""

    blueprint = forms.IntegerField(label="Cardmarket product", min_value=1)
    condition = forms.ChoiceField(choices=Condition.choices, initial=Condition.NEAR_MINT, required=False)
    language = forms.ChoiceField(choices=Language.choices, initial=Language.EN, required=False)
    foil = forms.BooleanField(required=False)
    quantity = forms.IntegerField(min_value=1, initial=1)

    def __init__(self, *args, owner, **kwargs):
        super().__init__(*args, **kwargs)
        self.owner = owner

    def clean_blueprint(self):
        bp = Blueprint.objects.select_related("expansion", "category").filter(cm_product_id=self.cleaned_data["blueprint"]).first()
        if bp is None:
            raise forms.ValidationError("Unknown catalog product.")
        return bp

    def _default_condition(self):
        """The seller's default condition (settings), Near Mint when unset."""
        return seller_settings_of(self.owner).default_condition or Condition.NEAR_MINT

    def save(self):
        """(item, merged): an identical stack of the owner's gets the copies."""
        data = self.cleaned_data
        bp = data["blueprint"]
        condition, language, foil = data["condition"] or self._default_condition(), data["language"] or Language.EN, data["foil"]
        if bp.is_product:
            condition, foil = PRODUCT_CONDITION, False
        card = catalog.card_for_blueprint(bp)
        stack = {"owner": self.owner, "card": card, "condition": condition, "language": language, "foil": foil}
        # Older rows may hold duplicate stacks (the free-text form never merged): the oldest gets the copies.
        item = CollectionItem.objects.filter(**stack).order_by("pk").first()
        if item is None:
            return CollectionItem.objects.create(**stack, quantity=data["quantity"]), False
        CollectionItem.objects.filter(pk=item.pk).update(quantity=F("quantity") + data["quantity"], updated_at=Now())
        item.refresh_from_db()
        return item, True


def keep_absent(data, instance, names):
    """data with the instance's current value for each of names it doesn't send (optional fields left unchanged)."""
    if data is None or all(name in data for name in names):
        return data
    data = data.copy() if hasattr(data, "copy") else dict(data)
    for name in names:
        if name not in data:
            value = getattr(instance, name)
            data[name] = "" if value is None else value
    return data


class ItemForm(forms.ModelForm):
    """Edits the copies of a collection item; the product they are copies of never changes. cost and location are
    optional: left out, they keep their value."""

    OPTIONAL = ["cost", "location"]

    cost = forms.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0"), required=False,
                              help_text="Purchase price of one copy")

    class Meta:
        model = CollectionItem
        fields = ["condition", "language", "foil", "quantity", "cost", "location"]

    def __init__(self, data=None, *args, instance=None, **kwargs):
        super().__init__(keep_absent(data, instance, self.OPTIONAL), *args, instance=instance, **kwargs)

    def save(self, commit=True):
        if self.instance.card.is_product:
            self.instance.condition, self.instance.foil = PRODUCT_CONDITION, False
        return super().save(commit=commit)


class ListingForm(forms.ModelForm):
    class Meta:
        model = Listing
        fields = ["quantity", "price", "status"]

    def clean_quantity(self):
        qty = self.cleaned_data["quantity"]
        item = self.instance.item  # views always build the form with Listing(item=...)
        if qty > item.quantity:
            copies = "copy" if item.quantity == 1 else "copies"
            raise forms.ValidationError(f"You only own {item.quantity} {copies}.")
        if qty < 1:
            raise forms.ValidationError("List at least one copy.")
        return qty


class BulkForm(forms.Form):
    """An action on several collection items (their ids are validated by the view): set_location (location),
    delete, list or reprice (pct: percent over the Cardmarket trend, may be negative)."""

    ACTIONS = [("set_location", "Set location"), ("delete", "Delete"), ("list", "List"), ("reprice", "Reprice")]

    action = forms.ChoiceField(choices=ACTIONS)
    location = forms.CharField(max_length=40, required=False)
    pct = forms.DecimalField(max_digits=6, decimal_places=2, min_value=Decimal("-99"), max_value=Decimal("1000"),
                             required=False, help_text="list/reprice: percent over the trend (default 0)")
    markets = forms.MultipleChoiceField(choices=Market.choices, required=False,
                                        help_text='list/reprice: the markets to act on (default ["ct"])')


class CsvImportForm(forms.Form):
    file = forms.FileField(label="CSV file")
