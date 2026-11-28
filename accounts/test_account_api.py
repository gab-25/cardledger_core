"""Login by email and remember, password reset, me's counters and markets, seller
settings, the activity log."""

import re

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from ledger import services
from ledger.models import CardmarketPrice, Listing, SyncLog
from ledger.test_game_api import pokemon_setup

from .models import AuditEvent, Profile, SellerSettings, record


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
                                                             "copies": 2, "live_listings": 1, "orders_to_pack": 0,
                                                             "shipment_problems": 0, "rules_on": 0, "rules_total": 0, "lots_to_process": 0})
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
        return next(m for m in self.me()["markets"] if m["code"] == "ct")

    def test_market_status(self):
        """CardTrader's status is its connection's, like Cardmarket's: no log or token rule."""
        self.assertEqual(self.market()["status"], "off")
        conn = services.connection(self.user, "ct")
        self.assertEqual((self.market()["status"], self.market()["connected"]), ("never", True))
        services.mark_synced(self.user, "ct")
        self.assertEqual(self.market()["status"], "ok")
        SyncLog.objects.create(owner=self.user, message="CardTrader: sync failed (boom)", level=SyncLog.Level.ERROR)
        self.assertEqual(self.market()["status"], "ok")  # the log does not decide
        services.mark_synced(self.user, "ct", error="boom")
        self.assertEqual(self.market()["status"], "error")
        conn.refresh_from_db()
        conn.status = conn.Status.OFF
        conn.save()
        self.assertEqual((self.market()["status"], self.market()["connected"]), ("off", False))
        services.mark_synced(self.user, "ct")
        self.client.force_login(self.other)  # another user's state is not shared
        self.assertEqual(self.market()["status"], "off")


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
            "price_rounding": "", "reprice_before_sync": False})

    def test_partial_put(self):
        self.assertEqual(self.put({"hold_below_cost": True, "price_rounding": "0.10"}).status_code, 200)
        self.put({"auto_import_orders": False})
        self.assertEqual(self.client.get(self.url).json(), {
            "auto_import_orders": False, "push_prices": True, "hold_below_cost": True, "default_condition": "NM",
            "price_rounding": "0.10", "reprice_before_sync": False})
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
        for path in ["/api/auth/password-reset/", "/api/auth/password-reset/confirm/",
                     "/api/account/settings/", "/api/account/activity/",
                     "/api/catalog/report-missing/"]:
            self.assertIn(path, schema["paths"])
        for path in ["/api/account/business/", "/api/account/business/logo/"]:  # single user: no business details
            self.assertNotIn(path, schema["paths"])

