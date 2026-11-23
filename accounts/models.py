from django.conf import settings
from django.db import models


class Profile(models.Model):
    """A user's business details (shop name, VAT number), created on demand by profile_of()."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    shop_name = models.CharField(max_length=120, blank=True)
    vat_number = models.CharField("VAT number", max_length=40, blank=True)

    def __str__(self):
        return self.shop_name or f"Profile of {self.user}"


def profile_of(user, create=False):
    """The user's Profile; an unsaved blank one when they have none (create=True saves it)."""
    if create:
        return Profile.objects.get_or_create(user=user)[0]
    return Profile.objects.filter(user=user).first() or Profile(user=user)
