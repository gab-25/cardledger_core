from django.urls import path

from . import api

app_name = "accounts"

urlpatterns = [
    path("api/me/", api.me, name="api_me"),
    path("api/auth/csrf/", api.csrf, name="api_csrf"),
    path("api/auth/login/", api.login_view, name="api_login"),
    path("api/auth/logout/", api.logout_view, name="api_logout"),
    path("api/auth/password-reset/", api.password_reset, name="api_password_reset"),
    path("api/auth/password-reset/confirm/", api.password_reset_confirm, name="api_password_reset_confirm"),
    path("api/account/", api.account, name="api_account"),
    path("api/account/profile/", api.profile, name="api_profile"),
    path("api/account/password/", api.password, name="api_password"),
    path("api/account/business/", api.business, name="api_business"),
    path("api/account/business/logo/", api.business_logo, name="api_business_logo"),
    path("api/account/settings/", api.seller_settings, name="api_settings"),
    path("api/account/activity/", api.activity, name="api_activity"),
]
