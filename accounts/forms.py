from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm

from ledger.forms import keep_absent
from ledger.models import Condition

from .models import BusinessDetails, Profile, SellerSettings

PRICE_ROUNDINGS = [("", "Default (0.05)"), ("0.05", "0.05"), ("0.10", "0.10"), ("0.50", "0.50"), ("1.00", "1.00")]
LOGO_MAX_BYTES = 2 * 1024 * 1024
# Signatures of the raster images taken as a logo (no SVG: it could carry scripts).
IMAGE_SIGNATURES = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpg", b"GIF87a": "gif", b"GIF89a": "gif"}


class ProfileForm(forms.ModelForm):
    class Meta:
        model = get_user_model()
        fields = ["first_name", "last_name", "email"]

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if email:
            taken = get_user_model().objects.filter(email__iexact=email).exclude(pk=self.instance.pk)
            if taken.exists():
                raise forms.ValidationError("This email is already used by another account.")
        return email


class BusinessProfileForm(forms.ModelForm):
    """The business details of the profile; left out of the request, a field keeps its value."""

    OPTIONAL = ["shop_name", "vat_number"]

    class Meta:
        model = Profile
        fields = ["shop_name", "vat_number"]

    def __init__(self, data=None, *args, instance=None, **kwargs):
        super().__init__(keep_absent(data, instance, self.OPTIONAL), *args, instance=instance, **kwargs)


class LoginForm(AuthenticationForm):
    """Django's login form taking the username or the email address (case-insensitive) in `username`, and remember.

    An email shared by several users matches none of them (the usual invalid-login error).
    """

    remember = forms.BooleanField(required=False, initial=True,
                                  help_text="Stay logged in (default); false: the session ends with the browser")

    def clean(self):
        login = (self.cleaned_data.get("username") or "").strip()
        User = get_user_model()
        if login and "@" in login and not User.objects.filter(**{User.USERNAME_FIELD: login}).exists():
            users = list(User.objects.filter(email__iexact=login)[:2])
            if len(users) == 1:
                self.cleaned_data["username"] = users[0].get_username()
        return super().clean()

    def target_user(self):
        """The account the attempt names (by username or unique email), None when there is none."""
        User = get_user_model()
        login = (self.cleaned_data.get("username") or self.data.get("username") or "").strip()
        if not login:
            return None
        user = User.objects.filter(**{User.USERNAME_FIELD: login}).first()
        if user is None and "@" in login:
            users = list(User.objects.filter(email__iexact=login)[:2])
            user = users[0] if len(users) == 1 else None
        return user


class BusinessForm(forms.ModelForm):
    """The business details; left out of the request, a field keeps its value (partial PUT)."""

    class Meta:
        model = BusinessDetails
        fields = ["legal_name", "tax_code", "phone", "buyer_email", "website", "ship_street", "ship_city",
                  "ship_postcode", "ship_country", "invoice_prefix", "invoice_next_number", "invoice_footer"]

    def __init__(self, data=None, *args, instance=None, **kwargs):
        super().__init__(keep_absent(data, instance, self.Meta.fields), *args, instance=instance, **kwargs)
        self.fields["invoice_next_number"].min_value = 1
        self.fields["invoice_next_number"].validators.append(_min_one)


def _min_one(value):
    if value is not None and value < 1:
        raise forms.ValidationError("Ensure this value is greater than or equal to 1.")


class LogoForm(forms.Form):
    logo = forms.FileField(help_text="PNG, JPEG or GIF image, at most 2 MB")

    def clean_logo(self):
        logo = self.cleaned_data["logo"]
        if logo.size > LOGO_MAX_BYTES:
            raise forms.ValidationError("The logo must be at most 2 MB.")
        head = logo.read(16)
        logo.seek(0)
        if not any(head.startswith(sig) for sig in IMAGE_SIGNATURES) and not (
                head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
            raise forms.ValidationError("Upload a PNG, JPEG, GIF or WebP image.")
        return logo


class SellerSettingsForm(forms.ModelForm):
    """The seller settings; left out of the request, a field keeps its value (partial PUT)."""

    default_condition = forms.ChoiceField(choices=Condition.choices)
    price_rounding = forms.ChoiceField(choices=PRICE_ROUNDINGS, required=False,
                                       help_text='Step of bulk prices; "": the default 0.05')

    class Meta:
        model = SellerSettings
        fields = ["auto_import_orders", "push_prices", "hold_below_cost", "default_condition", "price_rounding"]

    def __init__(self, data=None, *args, instance=None, **kwargs):
        super().__init__(keep_absent(data, instance, self.Meta.fields), *args, instance=instance, **kwargs)
