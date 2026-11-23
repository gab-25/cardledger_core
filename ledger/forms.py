from decimal import Decimal

from django import forms
from django.db.models import F
from django.db.models.functions import Now

from accounts.models import seller_settings_of

from . import catalog
from . import repricing
from .models import Blueprint, Card, CollectionItem, Condition, Language, Listing, Market, RepricingRule, Shipment


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
    cost = forms.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0"), required=False,
                              help_text="Purchase price of one copy (a merged stack keeps its own when blank)")
    location = forms.CharField(max_length=40, required=False, help_text="Storage location")
    list_on = forms.MultipleChoiceField(choices=Market.choices, required=False,
                                        help_text="Create listings (to publish) of the copies added on these markets, "
                                                  "at the price the repricing rules quote")

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
        extra = {"location": data["location"].strip()} if data.get("location", "").strip() else {}
        if data.get("cost") is not None:
            extra["cost"] = data["cost"]
        item = CollectionItem.objects.filter(**stack).order_by("pk").first()
        if item is None:
            return CollectionItem.objects.create(**stack, quantity=data["quantity"], **extra), False
        CollectionItem.objects.filter(pk=item.pk).update(quantity=F("quantity") + data["quantity"], updated_at=Now(),
                                                         **extra)
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


class PickedForm(forms.Form):
    """Ticks (or unticks) an order line as picked."""

    picked = forms.BooleanField(required=False)

    def clean(self):
        if "picked" not in self.data:
            self.add_error("picked", "This field is required.")
        return super().clean()


class PackForm(forms.Form):
    force = forms.BooleanField(required=False, help_text="Pack even if some lines are not picked")


class IssueForm(forms.Form):
    note = forms.CharField(max_length=1000, required=False, help_text="The problem; blank clears it")


class ShipmentForm(forms.ModelForm):
    """Edits a shipment: a field left out keeps its value; delivered/returned (booleans) mark it (now) or clear the
    mark when sent."""

    OPTIONAL = ["carrier", "tracking_code", "postage_cost", "note"]

    postage_cost = forms.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0"), required=False)
    delivered = forms.BooleanField(required=False, help_text="Delivered (now) or not; left out: unchanged")
    returned = forms.BooleanField(required=False, help_text="Returned (now) or not; left out: unchanged")

    class Meta:
        model = Shipment
        fields = ["carrier", "tracking_code", "postage_cost", "note"]

    def __init__(self, data=None, *args, instance=None, **kwargs):
        super().__init__(keep_absent(data, instance, self.OPTIONAL), *args, instance=instance, **kwargs)

    def save(self, commit=True):
        from django.utils import timezone

        shipment = self.instance
        if "delivered" in self.data:
            if self.cleaned_data["delivered"]:
                shipment.delivered_at = shipment.delivered_at or timezone.now()
                shipment.returned_at = None
            else:
                shipment.delivered_at = None
        if "returned" in self.data:
            if self.cleaned_data["returned"]:
                shipment.returned_at = shipment.returned_at or timezone.now()
            else:
                shipment.returned_at = None
        return super().save(commit=commit)


def _condition_error(index, text):
    return forms.ValidationError(f"Condition {index}: {text}")


def clean_conditions(value):
    """The conditions of a repricing rule, normalized: [{field, op, value}] with value a list of strings (in), a
    boolean (eq), an amount as a string (gte/lte) or [min, max] (between)."""
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise forms.ValidationError("A list of {field, op, value}.")
    cleaned = []
    for index, cond in enumerate(value, start=1):
        if not isinstance(cond, dict) or set(cond) - {"field", "op", "value"}:
            raise _condition_error(index, "an object with field, op and value.")
        name, op, raw = cond.get("field"), cond.get("op"), cond.get("value")
        if name not in repricing.CONDITION_OPS:
            raise _condition_error(index, f"unknown field '{name}'.")
        if op not in repricing.CONDITION_OPS[name]:
            raise _condition_error(index, f"{name} takes {', '.join(repricing.CONDITION_OPS[name])}.")
        if op == "in":
            if not isinstance(raw, list) or not raw or not all(isinstance(v, str) and v.strip() for v in raw):
                raise _condition_error(index, "a non-empty list of values.")
            raw = [v.strip() for v in raw]
        elif op == "eq":
            if not isinstance(raw, bool):
                raise _condition_error(index, "true or false.")
        else:
            values = raw if op == "between" else [raw]
            if op == "between" and (not isinstance(raw, list) or len(raw) != 2):
                raise _condition_error(index, "[min, max].")
            amounts = []
            for v in values:
                try:
                    amount = Decimal(str(v))
                except ArithmeticError:
                    raise _condition_error(index, f"'{v}' is not a number.") from None
                if not amount.is_finite() or amount < 0:
                    raise _condition_error(index, f"'{v}' is not a number.")
                amounts.append(amount)
            if op == "between" and amounts[0] > amounts[1]:
                raise _condition_error(index, "min is over max.")
            raw = [str(a) for a in amounts] if op == "between" else str(amounts[0])
        cleaned.append({"field": name, "op": op, "value": raw})
    return cleaned


class RuleForm(forms.ModelForm):
    """A repricing rule; a field left out keeps its value (on create: the model's default)."""

    FIELDS = ["name", "kind", "markets", "conditions", "reference", "adjust_pct", "adjust_abs", "round_to",
              "interval_days", "enabled", "draft"]

    conditions = forms.JSONField(required=False, help_text="[{field, op, value}]; empty: any listing")
    position = forms.IntegerField(min_value=1, required=False, help_text="1-based order; absent: last (on create) "
                                                                         "or unchanged")
    adjust_pct = forms.DecimalField(max_digits=7, decimal_places=2, min_value=Decimal("-99"),
                                    max_value=Decimal("1000"), required=False)
    adjust_abs = forms.DecimalField(max_digits=8, decimal_places=2, min_value=Decimal("-100000"),
                                    max_value=Decimal("100000"), required=False)
    interval_days = forms.IntegerField(min_value=1, max_value=365, required=False)

    class Meta:
        model = RepricingRule
        fields = ["name", "kind", "markets", "conditions", "reference", "adjust_pct", "adjust_abs", "round_to",
                  "interval_days", "enabled", "draft"]

    def __init__(self, data=None, *args, instance=None, **kwargs):
        super().__init__(keep_absent(data, instance, self.FIELDS), *args, instance=instance, **kwargs)

    def clean_conditions(self):
        return clean_conditions(self.cleaned_data.get("conditions"))

    def clean_adjust_pct(self):
        return self.cleaned_data.get("adjust_pct") or Decimal("0")

    def clean_adjust_abs(self):
        return self.cleaned_data.get("adjust_abs") or Decimal("0")

    def clean(self):
        data = super().clean()
        if data.get("kind") == RepricingRule.Kind.OVER_TIME:
            data["reference"] = RepricingRule.Reference.CURRENT
            if not data.get("interval_days"):
                self.add_error("interval_days", "An over-time rule needs its interval in days.")
        else:
            data["interval_days"] = None
        return data


class AdhocForm(forms.Form):
    """An ad-hoc reprice of collection items (their ids are validated by the view)."""

    MODES = [("trend_pct", "Percent of the basis"), ("change", "Change the basis by"), ("set", "Set the price")]
    BASES = [("trend", "Cardmarket trend"), ("low", "Cardmarket low"), ("avg30", "Cardmarket 30-day average"),
             ("current", "Current price")]

    mode = forms.ChoiceField(choices=MODES)
    value = forms.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("-100000"),
                               max_value=Decimal("100000"),
                               help_text="trend_pct: percent over the basis; change: the amount (unit); set: the price")
    unit = forms.ChoiceField(choices=[("pct", "Percent"), ("eur", "Euro")], required=False,
                             help_text='change: "pct" (default) or "eur"')
    basis = forms.ChoiceField(choices=BASES, required=False, help_text="Default trend")
    markets = forms.MultipleChoiceField(choices=Market.choices)
    dry_run = forms.BooleanField(required=False, help_text="true: only what it would do")

    def clean(self):
        data = super().clean()
        data["unit"] = data.get("unit") or "pct"
        data["basis"] = data.get("basis") or "trend"
        value = data.get("value")
        if value is not None:
            if data.get("mode") == "set" and value <= 0:
                self.add_error("value", "The price must be over 0.")
            elif data.get("mode") == "trend_pct" and value <= -100:
                self.add_error("value", "Over -100%.")
        return data
