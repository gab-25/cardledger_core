from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from .models import Profile


class ProfileTest(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("gab", email="gab@example.com", password="old-pass-123")
        User.objects.create_user("other", email="other@example.com", password="x")
        self.client.force_login(self.user)

    def put(self, name, data):
        return self.client.put(reverse(f"accounts:{name}"), data, content_type="application/json")

    def test_requires_login(self):
        self.client.logout()
        resp = self.client.get(reverse("accounts:api_me"))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "not_authenticated")

    def test_me_returns_user_and_counts(self):
        data = self.client.get(reverse("accounts:api_me")).json()
        self.assertEqual(data["user"]["username"], "gab")
        self.assertEqual(data["user"]["full_name"], "")
        self.assertFalse(data["user"]["is_staff"])
        self.assertEqual(data["counts"], {"pending_changes": 0, "orders_to_ship": 0, "copies": 0, "live_listings": 0,
                                          "orders_to_pack": 0, "shipment_problems": 0, "rules_on": 0, "rules_total": 0, "lots_to_process": 0})
        self.assertEqual(data["markets"], [
            {"code": "cm", "name": "Cardmarket", "connected": False, "status": "off", "last_sync": None, "handle": ""},
            {"code": "ct", "name": "CardTrader", "connected": False, "status": "off", "last_sync": None, "handle": ""},
        ])
        self.assertEqual(data["extension"], {"paired": False, "last_seen_at": None})
        self.assertIsNone(data["prices_updated_at"])

    def test_pending_changes_per_game(self):
        from ledger.models import Listing
        from ledger.test_game_api import pokemon_setup
        _, _, item = pokemon_setup(self)
        Listing.objects.create(item=item, price="1.00", quantity=1, status=Listing.Status.TO_PUBLISH)
        url = reverse("accounts:api_me")
        self.assertEqual(self.client.get(url, {"game": "pokemon"}).json()["counts"]["pending_changes"], 1)
        self.assertEqual(self.client.get(url, {"game": "mtg"}).json()["counts"]["pending_changes"], 0)
        self.assertEqual(self.client.get(url).json()["counts"]["pending_changes"], 1)
        self.assertEqual(self.client.get(url, {"game": ""}).json()["counts"]["pending_changes"], 1)
        self.assertEqual(self.client.get(url, {"game": "chess"}).status_code, 400)

    def test_account_has_only_the_password_rules(self):
        data = self.client.get(reverse("accounts:api_account")).json()
        self.assertEqual(set(data), {"password_help"})
        self.assertIn("Your password must contain at least 8 characters.", data["password_help"])

    def test_update_details(self):
        resp = self.put("api_profile", {"first_name": "Gabriele", "last_name": "S", "email": "new@example.com"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["messages"], [{"level": "success", "text": "Profile updated."}])
        self.user.refresh_from_db()
        self.assertEqual(self.user.get_full_name(), "Gabriele S")
        self.assertEqual(self.user.email, "new@example.com")

    def test_business_profile(self):
        user = self.client.get(reverse("accounts:api_me")).json()["user"]
        self.assertEqual((user["shop_name"], user["vat_number"]), ("", ""))
        self.assertFalse(Profile.objects.exists())  # created on demand, not by reading
        resp = self.put("api_profile", {"email": "gab@example.com", "shop_name": "Gab Cards",
                                        "vat_number": "IT01234567890"})
        self.assertEqual(resp.status_code, 200)
        user = self.client.get(reverse("accounts:api_me")).json()["user"]
        self.assertEqual((user["shop_name"], user["vat_number"]), ("Gab Cards", "IT01234567890"))
        # left out: unchanged
        self.put("api_profile", {"first_name": "Gabriele", "email": "gab@example.com"})
        profile = Profile.objects.get(user=self.user)
        self.assertEqual((profile.shop_name, profile.vat_number), ("Gab Cards", "IT01234567890"))
        self.assertEqual(Profile.objects.count(), 1)

    def test_business_profile_errors_with_user_errors(self):
        resp = self.put("api_profile", {"email": "OTHER@example.com", "vat_number": "X" * 41})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(set(resp.json()), {"email", "vat_number"})
        self.assertFalse(Profile.objects.exists())

    def test_email_must_be_unique(self):
        resp = self.put("api_profile", {"email": "OTHER@example.com"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), {"email": ["This email is already used by another account."]})

    def test_change_password_keeps_session(self):
        resp = self.client.post(reverse("accounts:api_password"), {
            "old_password": "old-pass-123", "new_password1": "N3w-secret-pass!", "new_password2": "N3w-secret-pass!",
        }, content_type="application/json")
        self.assertEqual(resp.json()["messages"][0]["text"], "Password changed.")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("N3w-secret-pass!"))
        self.assertEqual(self.client.get(reverse("accounts:api_me")).status_code, 200)  # still logged in

    def test_wrong_old_password(self):
        resp = self.client.post(reverse("accounts:api_password"), {
            "old_password": "nope", "new_password1": "N3w-secret-pass!", "new_password2": "N3w-secret-pass!",
        }, content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("old_password", resp.json())
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("old-pass-123"))


class LoginTest(TestCase):
    def setUp(self):
        get_user_model().objects.create_user("gab", password="pw-123456")
        self.client = Client(enforce_csrf_checks=True)

    def csrf(self):
        token = self.client.cookies.get("csrftoken")
        return token.value if token else ""

    def login(self, password, csrf=True):
        headers = {"HTTP_X_CSRFTOKEN": self.csrf()} if csrf else {}
        return self.client.post(reverse("accounts:api_login"), {"username": "gab", "password": password},
                                content_type="application/json", **headers)

    def test_login_ok_sets_session(self):
        self.client.get(reverse("accounts:api_csrf"))
        resp = self.login("pw-123456")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get(reverse("accounts:api_me")).json()["user"]["username"], "gab")

    def test_login_wrong_password(self):
        self.client.get(reverse("accounts:api_csrf"))
        resp = self.login("nope")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Please enter a correct username and password", resp.json()["non_field_errors"][0])

    def test_login_needs_csrf(self):
        self.client.get(reverse("accounts:api_csrf"))
        resp = self.login("pw-123456", csrf=False)
        self.assertEqual(resp.status_code, 403)

    def test_logout_ends_session(self):
        self.client.get(reverse("accounts:api_csrf"))
        self.login("pw-123456")
        resp = self.client.post(reverse("accounts:api_logout"), HTTP_X_CSRFTOKEN=self.csrf())
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(self.client.get(reverse("accounts:api_me")).status_code, 403)

    def test_schema_documents_the_login_body(self):
        resp = self.client.get(reverse("api_schema"), HTTP_ACCEPT="application/vnd.oai.openapi+json")
        schema = resp.json()
        body = schema["paths"]["/api/auth/login/"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        login = schema["components"]["schemas"][body["$ref"].rsplit("/", 1)[1]]
        self.assertEqual(set(login["properties"]), {"username", "password", "remember"})
        self.assertEqual(set(login["required"]), {"username", "password"})


class NoCardTraderTokenTest(TestCase):
    """CardTrader is synced by the browser extension: no token is stored or asked for anywhere."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("gab", password="pw")
        self.client.force_login(self.user)

    def test_token_endpoint_is_gone(self):
        for method in ("get", "put", "delete"):
            with self.subTest(method=method):
                resp = getattr(self.client, method)("/api/account/cardtrader-token/", {"token": "t"},
                                                    content_type="application/json")
                self.assertEqual(resp.status_code, 404)

    def test_cardtrader_is_off_until_the_extension_is_paired(self):
        ct = self.client.get(reverse("accounts:api_me")).json()["markets"][1]
        self.assertEqual((ct["code"], ct["status"], ct["connected"]), ("ct", "off", False))

