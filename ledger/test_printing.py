from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.urls import reverse

from decimal import Decimal

from .catalog import merge_cards, normalize_number
from .models import Card, CollectionItem, Listing
from .tests import LedgerTestCase, cm_id, load_catalog, game


class PrintingModelTest(LedgerTestCase):
    def test_two_printings_same_name_coexist(self):
        Card.objects.create(game=game(), name="Fable of the Mirror-Breaker", set_code="NEO", collector_number="141")
        Card.objects.create(game=game(), name="Fable of the Mirror-Breaker", set_code="NEO", collector_number="356")
        self.assertEqual(Card.objects.filter(name="Fable of the Mirror-Breaker").exclude(collector_number="").count(), 2)

    def test_same_set_number_rejected(self):
        Card.objects.create(game=game(), name="Fable of the Mirror-Breaker", set_code="NEO", collector_number="141")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Card.objects.create(game=game(), name="Other", set_code="NEO", collector_number="141")

    def test_legacy_rows_still_unique_by_name(self):
        Card.objects.create(game=game(), name="X", set_code="M10")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Card.objects.create(game=game(), name="X", set_code="M10")

    def test_str_shows_number(self):
        card = Card(name="Fable of the Mirror-Breaker", set_code="NEO", collector_number="356")
        self.assertEqual(str(card), "Fable of the Mirror-Breaker (NEO #356)")


class PrintingFormCsvTest(LedgerTestCase):
    def test_catalog_add_targets_printing(self):
        load_catalog()
        resp = self.client.post(reverse("ledger:api_collection"), {
            "blueprint": cm_id(1101), "condition": "NM", "language": "EN", "quantity": 1, "price": "10.00",
        }, content_type="application/json")
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(Card.objects.filter(set_code="NEO", collector_number="356", cm_product_id=cm_id(1101)).exists())

    def test_csv_number_column_roundtrip(self):
        upload = SimpleUploadedFile("c.csv", b"name,set,number,quantity\nFable of the Mirror-Breaker,neo,141,2\n")
        self.client.post(reverse("ledger:api_import"), {"file": upload})
        self.assertTrue(Card.objects.filter(name="Fable of the Mirror-Breaker", set_code="NEO",
                                            collector_number="141").exists())
        exported = self.client.get(reverse("ledger:api_export")).content.decode()
        self.assertIn("quantity,number", exported.splitlines()[0])
        self.assertIn(",141", exported)

    def test_normalize_number(self):
        self.assertEqual(normalize_number("0187"), "187")
        self.assertEqual(normalize_number("012a"), "12A")
        self.assertEqual(normalize_number(" 7 "), "7")
        self.assertEqual(normalize_number(""), "")


class MergeCardsTest(LedgerTestCase):
    def test_merge_refuses_two_live_listings(self):
        legacy = Card.objects.get(name="Counterspell", set_code="DMR")
        printing = Card.objects.create(game=game(), name="Counterspell", set_code="DMR", collector_number="45",
                                       ct_blueprint_id=1201)
        item = CollectionItem.objects.create(owner=self.user, card=printing, condition="NM", language="IT", quantity=1)
        Listing.objects.create(item=item, quantity=1, price=Decimal("1.00"))
        self.assertFalse(merge_cards(legacy, printing))
        self.assertTrue(Card.objects.filter(pk=legacy.pk).exists())
