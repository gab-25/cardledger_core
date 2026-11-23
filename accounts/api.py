"""The account API (/api/me/, /api/auth/, /api/account/): who is logged in, login/logout, password reset, profile,
business details, seller settings and the activity (security) log.

Authentication is the Django session; CSRF comes from the csrftoken cookie, sent back as X-CSRFToken.
"""

from django.contrib.auth import get_user_model, login, logout, password_validation, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm, PasswordResetForm, SetPasswordForm
from django.contrib.auth.tokens import default_token_generator
from django.core.files.storage import default_storage
from django.db.models import Max, Sum
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from rest_framework import serializers, status
from rest_framework.decorators import api_view, authentication_classes, parser_classes, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from ledger import services as ledger_services
from ledger.api.common import (GAME, doc, form_body, form_errors, form_fields, game_param, messages_response,
                               msg)
from ledger.api.orders import to_pack_count
from ledger.api.shipments import problem_count
from ledger.extension import extension_state
from ledger.models import CardmarketPrice, CollectionItem, Listing, Order, PurchaseLot, RepricingRule

from .forms import BusinessForm, BusinessProfileForm, LoginForm, LogoForm, ProfileForm, SellerSettingsForm
from .models import AuditEvent, business_of, profile_of, record, seller_settings_of

TAGS = ["account"]


def _user(user):
    profile = profile_of(user)
    return {
        "username": user.get_username(),
        "first_name": user.first_name,
        "last_name": user.last_name,
        "full_name": user.get_full_name(),
        "email": user.email,
        "is_staff": user.is_staff,
        "date_joined": user.date_joined,
        "last_login": user.last_login,
        "shop_name": profile.shop_name,
        "vat_number": profile.vat_number,
    }


@doc(TAGS, "The logged-in user, the sidebar counters (?game limits pending_changes, copies and live_listings to a "
           "game; orders_to_pack: to pack or being picked, shipment_problems: late or untracked past their expected "
           "date, both of every market; rules_on: enabled repricing rules that are not drafts, rules_total: all "
           "of them; lots_to_process: purchase lots in the post, received or grading), the marketplaces' state (Cardmarket and CardTrader, with the seller handle), whether the browser "
           "extension is paired (and when it was last seen) and the date of the latest Cardmarket price guide. "
           "Each market's status is its connection's (both are synced by the extension): off until the extension is "
           "paired, never (never synced), ok, error (the last sync failed)",
     parameters=[GAME])
@api_view(["GET"])
def me(request):
    user = request.user
    pending = Listing.objects.owned_by(user).filter(status=Listing.Status.TO_PUBLISH)
    live = Listing.objects.owned_by(user).filter(status=Listing.Status.PUBLISHED, pending_delete=False)
    items = CollectionItem.objects.owned_by(user)
    rules = RepricingRule.objects.owned_by(user)
    if request.query_params.get("game", "").strip():
        game = game_param(request.query_params)
        pending = pending.filter(item__card__game=game)
        live = live.filter(item__card__game=game)
        items = items.filter(card__game=game)
    return Response({
        "user": _user(user),
        "counts": {
            "pending_changes": pending.count(),
            "orders_to_ship": Order.objects.owned_by(user).filter(state=Order.State.PAID).count(),
            "copies": items.aggregate(n=Sum("quantity"))["n"] or 0,
            "live_listings": live.count(),
            "orders_to_pack": to_pack_count(user),
            "shipment_problems": problem_count(user),
            "rules_on": rules.filter(enabled=True, draft=False).count(),
            "rules_total": rules.count(),
            "lots_to_process": PurchaseLot.objects.owned_by(user).filter(status__in=PurchaseLot.TO_PROCESS).count(),
        },
        "markets": ledger_services.market_states(user),
        "extension": extension_state(user),
        "prices_updated_at": CardmarketPrice.objects.aggregate(d=Max("prices_date"))["d"],
    })


@doc(TAGS, "Set the CSRF cookie (for the login form)")
@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
@ensure_csrf_cookie
def csrf(request):
    return Response(status=status.HTTP_204_NO_CONTENT)


@doc(TAGS, "Log in with the username or the email address (case-insensitive; an email shared by several accounts "
           "is refused) and password; remember (default true) false ends the session with the browser",
     request=form_body("Login", LoginForm))
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@csrf_protect
def login_view(request):
    form = LoginForm(request, data=request.data)
    if not form.is_valid():
        target = form.target_user()
        if target is not None:
            record(target, AuditEvent.Kind.SIGN_IN_FAILED, request)
        raise form_errors(form)
    user = form.get_user()
    login(request, user)
    if "remember" in request.data and not form.cleaned_data["remember"]:
        request.session.set_expiry(0)  # until the browser closes
    record(user, AuditEvent.Kind.SIGN_IN, request)
    return Response({"ok": True})


RESET_DONE = "If an account uses this email, we sent it a link to choose a new password."


@doc(TAGS, "Send a password reset link to the email (always answers the same message: it never tells whether an "
           "account uses it); the link opens /login/reset/<uid>/<token>/",
     request=form_body("PasswordReset", PasswordResetForm))
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@csrf_protect
def password_reset(request):
    form = PasswordResetForm(request.data)
    if not form.is_valid():
        raise form_errors(form)
    form.save(
        request=request, use_https=request.is_secure(),
        subject_template_name="accounts/password_reset_subject.txt",
        email_template_name="accounts/password_reset_email.txt",
    )
    return messages_response(msg("success", RESET_DONE))


def _reset_user(uid, token):
    try:
        user = get_user_model()._default_manager.get(pk=force_str(urlsafe_base64_decode(uid)))
    except (TypeError, ValueError, OverflowError, get_user_model().DoesNotExist):
        return None
    return user if default_token_generator.check_token(user, token) else None


@doc(TAGS, "Choose a new password from a reset link (uid and token of /login/reset/<uid>/<token>/)",
     request=form_body("PasswordResetConfirm", SetPasswordForm, uid=serializers.CharField(),
                       token=serializers.CharField()))
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@csrf_protect
def password_reset_confirm(request):
    user = _reset_user(str(request.data.get("uid", "")), str(request.data.get("token", "")))
    if user is None:
        raise ValidationError({"token": ["This password reset link is invalid or has expired: ask for a new one."]})
    form = SetPasswordForm(user, request.data)
    if not form.is_valid():
        raise form_errors(form)
    form.save()
    record(user, AuditEvent.Kind.PASSWORD_RESET, request)
    return messages_response(msg("success", "Password changed: you can now log in with the new one."))


@doc(TAGS, "Log out")
@api_view(["POST"])
def logout_view(request):
    logout(request)
    return Response(status=status.HTTP_204_NO_CONTENT)


@doc(TAGS, "Profile page data: the password rules")
@api_view(["GET"])
def account(request):
    return Response({"password_help": password_validation.password_validators_help_texts()})


@doc(TAGS, "Change first name, last name and email, and the business details (shop name, VAT number: left out, "
           "they keep their value)",
     request=form_body("Profile", ProfileForm, **form_fields(BusinessProfileForm)))
@api_view(["PUT"])
def profile(request):
    form = ProfileForm(request.data, instance=request.user)
    business = BusinessProfileForm(request.data, instance=profile_of(request.user))
    valid = form.is_valid()
    if not business.is_valid():
        form.errors.update(business.errors)
        valid = False
    if not valid:
        raise form_errors(form)
    form.save()
    business.save()
    return messages_response(msg("success", "Profile updated."))


@doc(TAGS, "Change the password (the session stays logged in)",
     request=form_body("PasswordChange", PasswordChangeForm))
@api_view(["POST"])
def password(request):
    form = PasswordChangeForm(request.user, request.data)
    if not form.is_valid():
        raise form_errors(form)
    form.save()
    update_session_auth_hash(request, form.user)  # stay logged in
    record(request.user, AuditEvent.Kind.PASSWORD_CHANGED, request)
    return messages_response(msg("success", "Password changed."))


def business_data(user):
    """The business details as GET /api/account/business/ answers them (also the packing slips' header)."""
    profile, business = profile_of(user), business_of(user)
    data = {"shop_name": profile.shop_name, "vat_number": profile.vat_number}
    data.update({name: getattr(business, name) for name in BusinessForm.Meta.fields})
    data["logo_url"] = business.logo.url if business.logo else ""
    return data


@doc(TAGS, "The business details for packing slips and invoices (GET) or change them (PUT; a key left out keeps its "
           "value). shop_name and vat_number are the profile's",
     request=form_body("Business", BusinessForm, **form_fields(BusinessProfileForm)))
@api_view(["GET", "PUT"])
def business(request):
    user = request.user
    if request.method == "GET":
        return Response(business_data(user))
    form = BusinessForm(request.data, instance=business_of(user))
    profile = BusinessProfileForm(request.data, instance=profile_of(user))
    valid = form.is_valid()
    if not profile.is_valid():
        form.errors.update(profile.errors)
        valid = False
    if not valid:
        raise form_errors(form)
    form.save()
    profile.save()
    return messages_response(msg("success", "Business details saved."))


@doc(TAGS, "Upload the business logo (POST multipart 'logo': PNG, JPEG, GIF or WebP, at most 2 MB; answers its "
           "logo_url) or remove it (DELETE)", request={"multipart/form-data": form_body("BusinessLogo", LogoForm)})
@api_view(["POST", "DELETE"])
@parser_classes([MultiPartParser])
def business_logo(request):
    details = business_of(request.user)
    old = details.logo.name if details.logo else ""
    if request.method == "DELETE":
        if old:
            details.logo = ""
            details.save(update_fields=["logo"])
            default_storage.delete(old)
        return messages_response(msg("success", "Logo removed."))
    form = LogoForm(data={}, files=request.FILES)
    if not form.is_valid():
        raise form_errors(form)
    details.logo = form.cleaned_data["logo"]
    details.save()
    if old and old != details.logo.name:
        default_storage.delete(old)
    return messages_response(msg("success", "Logo uploaded."), logo_url=details.logo.url)


def _seller_settings(settings):
    return {name: getattr(settings, name) for name in SellerSettingsForm.Meta.fields}


@doc(TAGS, "The seller settings (GET) or change them (PUT; a key left out keeps its value). hold_below_cost and "
           "price_rounding apply to the bulk list/reprice of the collection and to the repricing rules, "
           "default_condition to adding from the catalog, reprice_before_sync runs the rules before each extension "
           "plan", request=form_body("SellerSettings", SellerSettingsForm))
@api_view(["GET", "PUT"])
def seller_settings(request):
    settings = seller_settings_of(request.user)
    if request.method == "GET":
        return Response(_seller_settings(settings))
    form = SellerSettingsForm(request.data, instance=settings)
    if not form.is_valid():
        raise form_errors(form)
    form.save()
    return messages_response(msg("success", "Settings saved."))


ACTIVITY_SIZE = 50


@doc(TAGS, f"The account's security events, newest first (the last {ACTIVITY_SIZE}): sign-ins (ok and failed), "
           "password changes and resets (and the CardTrader token events of earlier versions)")
@api_view(["GET"])
def activity(request):
    events = AuditEvent.objects.filter(user=request.user)[:ACTIVITY_SIZE]
    return Response({"results": [
        {"at": e.at, "kind": e.kind, "text": e.text, "ip": e.ip or "", "device": e.device} for e in events
    ]})
