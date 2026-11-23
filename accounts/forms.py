from django import forms
from django.contrib.auth import get_user_model

from ledger.forms import keep_absent

from .models import Profile


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


class CardTraderTokenForm(forms.Form):
    token = forms.CharField(
        label="CardTrader API token",
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "off"}),
        help_text="Found in your CardTrader profile settings, under API. It is checked with CardTrader before saving.",
    )
