"""Scan sessions gain a game (migration 0033): the ones there already are Magic ones."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

AUTH = ("auth", "0012_alter_user_first_name_max_length")
BEFORE = [("ledger", "0032_longer_set_codes"), AUTH]
AFTER = [("ledger", "0033_scansession_game"), AUTH]


class ScanSessionGameMigrationTest(TransactionTestCase):
    def setUp(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(BEFORE)
        self.apps = self.executor.loader.project_state(BEFORE).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_existing_sessions_become_magic(self):
        get = self.apps.get_model
        # Seeded by 0028 on a real database; an earlier TransactionTestCase may have flushed it.
        get("ledger", "Game").objects.get_or_create(slug="mtg", defaults={"name": "Magic: The Gathering", "cm_id": 1})
        user = get("auth", "User").objects.create(username="gab")
        Session = get("ledger", "ScanSession")
        open_session = Session.objects.create(owner=user)
        saved_session = Session.objects.create(owner=user, status="saved")

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(AFTER)
        Session = executor.loader.project_state(AFTER).apps.get_model("ledger", "ScanSession")
        for session in (open_session, saved_session):
            self.assertEqual(Session.objects.get(pk=session.pk).game.slug, "mtg")
