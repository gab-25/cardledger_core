from django.contrib import admin

from .models import AuditEvent, BusinessDetails, Profile, SellerSettings


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ["user", "shop_name", "vat_number"]


@admin.register(BusinessDetails)
class BusinessDetailsAdmin(admin.ModelAdmin):
    list_display = ["user", "legal_name", "buyer_email"]


@admin.register(SellerSettings)
class SellerSettingsAdmin(admin.ModelAdmin):
    list_display = ["user", "auto_import_orders", "push_prices", "hold_below_cost", "price_rounding"]


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ["at", "user", "kind", "ip"]
    list_filter = ["kind"]
    readonly_fields = ["at"]
