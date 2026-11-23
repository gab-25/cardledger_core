"""Login by email and remember, password reset, me's counters and markets, business details and logo, seller
settings, the activity log."""

import re
import shutil
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ledger import services
from ledger.cardtrader import CardTraderError
from ledger.models import CardmarketPrice, Listing, SyncLog
from ledger.test_game_api import pokemon_setup

from .models import AuditEvent, BusinessDetails, Profile, SellerSettings, record
from .tests import fake_client

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class LoginTest(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("gab", email="Gab@Example.com", password="pw-123456")
        self.client = Client(enforce_csrf_checks=True, HTTP_USER_AGENT="TestBrowser/1.0")
        self.client.get(reverse("accounts:api_csrf"))

    def login(self, username, password="pw-123456", **extra):
        return self.client.post(reverse("accounts:api_login"), {"username": username, "password": password, **extra},
                                content_type="application/json",
                                HTTP_X_CSRFTOKEN=self.client.cookies["csrftoken"].value)

    def test_login_by_email_case_insensitive(self):
        self.assertEqual(self.login("gab@example.COM").status_code, 200)
        self.assertEqual(self.client.get(reverse("accounts:api_me")).json()["user"]["username"], "gab")

    def test_email_shared_by_two_users_is_refused(self):
        get_user_model().objects.create_user("gab2", email="gab@example.com", password="pw-123456")
        resp = self.login("gab@example.com")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Please enter a correct username and password", resp.json()["non_field_errors"][0])
        self.assertFalse(AuditEvent.objects.exists())  # no single account to record it on

    def test_remember_default_keeps_the_session(self):
        self.login("gab")
        self.assertFalse(self.client.session.get_expire_at_browser_close())

    def test_remember_false_ends_with_the_browser(self):
        self.login("gab", remember=False)
        self.assertTrue(self.client.session.get_expire_at_browser_close())

    def test_sign_ins_are_recorded(self):
        self.assertEqual(self.login("gab", password="wrong").status_code, 400)
        self.login("gab@example.com")
        events = list(AuditEvent.objects.filter(user=self.user).values_list("kind", "text", "ip", "device"))
        self.assertEqual(events, [
            ("sign_in", "Signed in", "127.0.0.1", "TestBrowser/1.0"),
            ("sign_in_failed", "Failed sign-in", "127.0.0.1", "TestBrowser/1.0"),
        ])

    def test_unknown_user_records_nothing(self):
        self.login("nobody", password="x")
        self.assertFalse(AuditEvent.objects.exists())


class PasswordResetTest(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("gab", email="gab@example.com", password="old-pass-123")

    def reset(self, email):
        return self.client.post(reverse("accounts:api_password_reset"), {"email": email},
                                content_type="application/json")

    def confirm(self, **data):
        return self.client.post(reverse("accounts:api_password_reset_confirm"), data, content_type="application/json")

    def link(self):
        self.reset("GAB@example.com")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["gab@example.com"])
        match = re.search(r"http://testserver/login/reset/([^/]+)/([^/]+)/", mail.outbox[0].body)
        self.assertIsNotNone(match, mail.outbox[0].body)
        return match.groups()

    def test_unknown_email_answers_the_same(self):
        known, unknown = self.reset("gab@example.com"), self.reset("nobody@example.com")
        self.assertEqual(known.status_code, 200)
        self.assertEqual(known.json(), unknown.json())
        self.assertEqual(known.json()["messages"][0]["level"], "success")
        self.assertEqual(len(mail.outbox), 1)

    def test_invalid_email_is_a_field_error(self):
        self.assertIn("email", self.reset("not-an-email").json())

    def test_confirm_sets_the_password(self):
        uid, token = self.link()
        resp = self.confirm(uid=uid, token=token, new_password1="brand-new-pw-9", new_password2="brand-new-pw-9")
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("brand-new-pw-9"))
        self.assertEqual(AuditEvent.objects.get(user=self.user).kind, "password_reset")
        # the link works once
        resp = self.confirm(uid=uid, token=token, new_password1="another-pw-77", new_password2="another-pw-77")
        self.assertIn("token", resp.json())

    def test_confirm_field_errors(self):
        uid, token = self.link()
        resp = self.confirm(uid=uid, token=token, new_password1="brand-new-pw-9", new_password2="different-pw-9")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("new_password2", resp.json())
        resp = self.confirm(uid=uid, token=token, new_password1="123", new_password2="123")
        self.assertIn("new_password2", resp.json())  # SetPasswordForm reports the validators on new_password2

    def test_bad_token(self):
        resp = self.confirm(uid="xx", token="nope", new_password1="brand-new-pw-9", new_password2="brand-new-pw-9")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("token", resp.json())


class MeTest(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("gab", password="pw")
        self.other = User.objects.create_user("ami", password="pw")
        self.client.force_login(self.user)

    def me(self, **params):
        return self.client.get(reverse("accounts:api_me"), params).json()

    def test_copies_and_live_listings_per_game(self):
        _, _, pikachu = pokemon_setup(self)  # 2 copies
        Listing.objects.create(item=pikachu, price="1.00", quantity=1, status=Listing.Status.PUBLISHED)
        self.assertEqual(self.me()["counts"]["copies"], 2)
        self.assertEqual(self.me(game="pokemon")["counts"], {"pending_changes": 0, "orders_to_ship": 0,
                                                             "copies": 2, "live_listings": 1})
        self.assertEqual(self.me(game="mtg")["counts"]["copies"], 0)
        self.assertEqual(self.me(game="mtg")["counts"]["live_listings"], 0)
        self.client.force_login(self.other)
        self.assertEqual(self.me()["counts"]["copies"], 0)
        self.assertEqual(self.me()["counts"]["live_listings"], 0)

    def test_prices_updated_at(self):
        when = timezone.now().replace(microsecond=0)
        CardmarketPrice.objects.create(id_product=1, trend="1.00", prices_date=when)
        self.assertEqual(self.me()["prices_updated_at"][:19], when.isoformat()[:19])

    def market(self):
        return self.me()["markets"][0]

    def test_market_status(self):
        self.assertEqual(self.market()["status"], "off")
        service = services.get_cardtrader(self.user)
        service.api_token = "tok"
        service.save()
        self.assertEqual((self.market()["status"], self.market()["connected"]), ("never", True))
        service.last_sync = timezone.now()
        service.save()
        SyncLog.objects.create(owner=self.user, message="CardTrader: listings in sync")
        self.assertEqual(self.market()["status"], "ok")
        SyncLog.objects.create(owner=self.user, message="CardTrader: sync failed (boom)", level=SyncLog.Level.ERROR)
        self.assertEqual(self.market()["status"], "error")
        SyncLog.objects.create(owner=None, message="Catalog sync: done")  # global: not CardTrader's
        self.assertEqual(self.market()["status"], "error")
        SyncLog.objects.create(owner=self.user, message="CardTrader: 2 listings published")
        self.assertEqual(self.market()["status"], "ok")
        self.client.force_login(self.other)  # another user's state is not shared
        self.assertEqual(self.market()["status"], "off")

    def test_failed_sync_is_logged_as_error(self):
        service = services.get_cardtrader(self.user)
        service.api_token, service.last_sync = "tok", timezone.now()
        service.save()
        with mock.patch.object(services, "make_client", side_effect=CardTraderError("unauthorized")):
            resp = self.client.post(reverse("ledger:api_sync_run"))
        self.assertEqual(resp.status_code, 502)
        log = SyncLog.objects.filter(owner=self.user).first()
        self.assertEqual((log.level, log.message), ("error", "CardTrader: sync failed (unauthorized)"))
        self.assertEqual(self.market()["status"], "error")


class BusinessTest(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("gab", password="pw")
        self.other = User.objects.create_user("ami", password="pw")
        self.client.force_login(self.user)
        self.url = reverse("accounts:api_business")
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)

    def put(self, data):
        return self.client.put(self.url, data, content_type="application/json")

    def test_empty(self):
        data = self.client.get(self.url).json()
        self.assertEqual(data["invoice_next_number"], 1)
        self.assertEqual(data["logo_url"], "")
        self.assertEqual(set(data), {
            "shop_name", "legal_name", "vat_number", "tax_code", "phone", "buyer_email", "website", "ship_street",
            "ship_city", "ship_postcode", "ship_country", "invoice_prefix", "invoice_next_number", "invoice_footer",
            "logo_url"})
        self.assertFalse(BusinessDetails.objects.exists())

    def test_partial_put(self):
        resp = self.put({"shop_name": "Gab Cards", "vat_number": "IT1", "legal_name": "Gab Srl",
                         "buyer_email": "shop@example.com", "website": "https://gab.example", "invoice_prefix": "GC-",
                         "invoice_next_number": 42, "ship_city": "Roma"})
        self.assertEqual(resp.status_code, 200, resp.json())
        self.assertEqual(resp.json()["messages"], [{"level": "success", "text": "Business details saved."}])
        self.put({"phone": "+39 06 1234"})  # the rest keeps its value
        data = self.client.get(self.url).json()
        self.assertEqual((data["shop_name"], data["vat_number"], data["legal_name"], data["invoice_next_number"],
                          data["ship_city"], data["phone"]), ("Gab Cards", "IT1", "Gab Srl", 42, "Roma", "+39 06 1234"))
        self.assertEqual(Profile.objects.get(user=self.user).shop_name, "Gab Cards")  # on the profile, as before
        self.assertEqual(self.client.get(reverse("accounts:api_me")).json()["user"]["shop_name"], "Gab Cards")
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url).json()["legal_name"], "")

    def test_validation(self):
        resp = self.put({"buyer_email": "nope", "website": "not a url", "invoice_next_number": 0})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(set(resp.json()), {"buyer_email", "website", "invoice_next_number"})
        self.assertFalse(BusinessDetails.objects.exists())

    def logo(self, content=PNG, name="logo.png", content_type="image/png"):
        return self.client.post(reverse("accounts:api_business_logo"),
                                {"logo": SimpleUploadedFile(name, content, content_type=content_type)})

    def test_logo_upload_and_remove(self):
        resp = self.logo()
        self.assertEqual(resp.status_code, 200, resp.json())
        url = resp.json()["logo_url"]
        self.assertTrue(url.startswith("/media/logos/"))
        self.assertEqual(self.client.get(self.url).json()["logo_url"], url)
        served = self.client.get(url)
        self.assertEqual((served.status_code, served["Content-Type"]), (200, "image/png"))
        self.assertEqual(b"".join(served.streaming_content), PNG)
        resp = self.client.delete(reverse("accounts:api_business_logo"))
        self.assertEqual(resp.json()["messages"], [{"level": "success", "text": "Logo removed."}])
        self.assertEqual(self.client.get(self.url).json()["logo_url"], "")
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_new_logo_replaces_the_old_file(self):
        first = self.logo().json()["logo_url"]
        second = self.logo().json()["logo_url"]
        self.assertNotEqual(first, second)
        self.assertEqual(self.client.get(first).status_code, 404)

    def test_logo_must_be_an_image_under_2mb(self):
        resp = self.logo(b"<svg onload=alert(1)>", "logo.svg", "image/svg+xml")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("logo", resp.json())
        resp = self.logo(PNG + b"\x00" * (2 * 1024 * 1024))
        self.assertEqual(resp.json()["logo"], ["The logo must be at most 2 MB."])
        self.assertIn("logo", self.client.post(reverse("accounts:api_business_logo"), {}).json())

    def test_logo_is_per_user(self):
        self.logo()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url).json()["logo_url"], "")
        self.client.delete(reverse("accounts:api_business_logo"))
        self.assertTrue(BusinessDetails.objects.get(user=self.user).logo)


class SellerSettingsTest(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("gab", password="pw")
        self.other = User.objects.create_user("ami", password="pw")
        self.client.force_login(self.user)
        self.url = reverse("accounts:api_settings")

    def put(self, data):
        return self.client.put(self.url, data, content_type="application/json")

    def test_defaults(self):
        self.assertEqual(self.client.get(self.url).json(), {
            "auto_import_orders": True, "push_prices": True, "hold_below_cost": False, "default_condition": "NM",
            "price_rounding": ""})

    def test_partial_put(self):
        self.assertEqual(self.put({"hold_below_cost": True, "price_rounding": "0.10"}).status_code, 200)
        self.put({"auto_import_orders": False})
        self.assertEqual(self.client.get(self.url).json(), {
            "auto_import_orders": False, "push_prices": True, "hold_below_cost": True, "default_condition": "NM",
            "price_rounding": "0.10"})
        self.client.force_login(self.other)
        self.assertFalse(self.client.get(self.url).json()["hold_below_cost"])

    def test_validation(self):
        resp = self.put({"default_condition": "ZZ", "price_rounding": "0.07"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(set(resp.json()), {"default_condition", "price_rounding"})
        for step in ["", "0.05", "0.10", "0.50", "1.00"]:
            self.assertEqual(self.put({"price_rounding": step}).status_code, 200, step)
        self.assertEqual(self.put({"price_rounding": "abc"}).status_code, 400)
        self.assertEqual(SellerSettings.objects.get(user=self.user).price_rounding, "1.00")


class ActivityTest(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("gab", password="old-pass-123")
        self.other = User.objects.create_user("ami", password="pw")
        self.client.force_login(self.user)

    def activity(self):
        return self.client.get(reverse("accounts:api_activity")).json()["results"]

    def test_password_change_is_recorded(self):
        self.client.post(reverse("accounts:api_password"), {
            "old_password": "old-pass-123", "new_password1": "brand-new-pw-9", "new_password2": "brand-new-pw-9",
        }, content_type="application/json")
        [event] = self.activity()
        self.assertEqual((event["kind"], event["text"], event["ip"]), ("password_changed", "Password changed",
                                                                       "127.0.0.1"))
        self.assertEqual(set(event), {"at", "kind", "text", "ip", "device"})

    @mock.patch.object(services, "make_client", side_effect=fake_client)
    def test_token_saved_and_removed(self, _):
        url = reverse("accounts:api_token")
        self.client.put(url, {"token": "good-token"}, content_type="application/json")
        self.client.put(url, {"token": "bad"}, content_type="application/json")  # rejected: nothing recorded
        self.client.delete(url)
        self.assertEqual([e["kind"] for e in self.activity()], ["token_removed", "token_saved"])

    def test_newest_first_last_50_own_only(self):
        for i in range(55):
            record(self.user, AuditEvent.Kind.SIGN_IN, text=f"event {i}")
        record(self.other, AuditEvent.Kind.SIGN_IN, text="not mine")
        events = self.activity()
        self.assertEqual(len(events), 50)
        self.assertEqual(events[0]["text"], "event 54")
        self.assertNotIn("not mine", [e["text"] for e in events])
        self.assertEqual(events[0]["ip"], "")  # recorded without a request


class SchemaDocsTest(TestCase):
    def test_new_endpoints_are_documented(self):
        schema = self.client.get(reverse("api_schema"), HTTP_ACCEPT="application/vnd.oai.openapi+json").json()
        for path in ["/api/auth/password-reset/", "/api/auth/password-reset/confirm/", "/api/account/business/",
                     "/api/account/business/logo/", "/api/account/settings/", "/api/account/activity/",
                     "/api/catalog/report-missing/"]:
            self.assertIn(path, schema["paths"])

