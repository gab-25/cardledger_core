"""The games API (/api/games/) and the shared ?game parameter."""

from django.test import Client
from django.urls import reverse

from .tests import LedgerTestCase

# Url names of the views that take ?game; every task that adds one appends it here.
GAME_VIEWS = ["ledger:api_catalog", "ledger:api_collection", "ledger:api_export", "ledger:api_listings", "accounts:api_me",
             "ledger:scan_api_setup", "ledger:scan_api_sessions"]


class GamesApiTest(LedgerTestCase):
    def test_lists_every_game(self):
        resp = self.client.get(reverse("ledger:api_games"))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual({g["slug"] for g in data}, {"mtg", "pokemon", "lorcana", "onepiece", "yugioh", "riftbound"})
        self.assertTrue(all(set(g) == {"slug", "name"} for g in data))
        self.assertEqual([g["name"] for g in data], sorted(g["name"] for g in data))

    def test_requires_login(self):
        self.assertEqual(Client().get(reverse("ledger:api_games")).status_code, 403)

    def test_unknown_game_is_400_everywhere(self):
        for name in GAME_VIEWS:
            with self.subTest(view=name):
                resp = self.client.get(reverse(name), {"game": "chess"})
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.json()["game"], ["Unknown game 'chess'."])
