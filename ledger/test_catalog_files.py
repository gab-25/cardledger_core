import hashlib
import io
import json
import os
from datetime import date
from unittest import mock

import boto3
from botocore.response import StreamingBody
from botocore.stub import Stubber
from django.test import SimpleTestCase, override_settings

from ledger.catalog_files import CatalogBucket, CatalogFileMissing, CatalogFilesError, catalog_key

BUCKET = "test-bucket"
DOC = {
    "expansions": [
        {"code": "1", "name": "First", "released_at": "2023-09-01", "language": ""},
        {"code": "2", "name": "Second", "released_at": None, "language": "en"},
    ],
    "products": [
        {"id_product": 10, "name": "Elsa", "expansion_code": "1", "number": "5", "rarity": "Rare", "image_url": "u",
         "image_large_url": "L", "category_id": 1, "category_name": "Cards"},
    ],
}


def catalog_json(doc):
    """A game file as the generator writes it: one uncompressed JSON object."""
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def body(data):
    return StreamingBody(io.BytesIO(data), len(data))


@override_settings(CARDLEDGER_CATALOG_BUCKET=BUCKET)
class CatalogBucketTest(SimpleTestCase):
    def setUp(self):
        self.client = boto3.client(
            "s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y"
        )
        self.stub = Stubber(self.client)
        self.stub.activate()
        self.addCleanup(self.stub.deactivate)

    def expect_get(self, key, data):
        self.stub.add_response(
            "get_object", {"Body": body(data)}, {"Bucket": BUCKET, "Key": key}
        )

    def expect_missing(self, key):
        self.stub.add_client_error(
            "get_object", service_error_code="NoSuchKey", http_status_code=404,
            expected_params={"Bucket": BUCKET, "Key": key},
        )

    def test_catalog_key(self):
        self.assertEqual(catalog_key("lorcana"), "catalog/v1/lorcana.json")

    def test_fetch_returns_the_raw_bytes(self):
        data = catalog_json(DOC)
        self.expect_get("catalog/v1/lorcana.json", data)
        self.assertEqual(CatalogBucket(client=self.client).fetch("lorcana"), data)

    def test_missing_object_raises_catalog_file_missing(self):
        self.expect_missing("catalog/v1/lorcana.json")
        with self.assertRaises(CatalogFileMissing) as raised:
            CatalogBucket(client=self.client).fetch("lorcana")
        self.assertIsInstance(raised.exception, CatalogFilesError)

    def test_other_read_errors_raise_catalog_files_error(self):
        self.stub.add_client_error("get_object", service_error_code="AccessDenied", http_status_code=403)
        with self.assertRaises(CatalogFilesError) as raised:
            CatalogBucket(client=self.client).fetch("lorcana")
        self.assertNotIsInstance(raised.exception, CatalogFileMissing)

    def parse(self, data):
        return CatalogBucket(client=self.client).parse("lorcana", data)

    def test_game_file_is_parsed(self):
        game = self.parse(catalog_json(DOC))
        self.assertEqual(game.slug, "lorcana")
        self.assertEqual([e.code for e in game.expansions], ["1", "2"])
        self.assertEqual(game.expansions[0].released_at, date(2023, 9, 1))
        self.assertIsNone(game.expansions[1].released_at)
        self.assertEqual(game.expansions[1].language, "en")
        (product,) = game.products
        self.assertEqual(product.id_product, 10)
        self.assertEqual(product.expansion_code, "1")
        self.assertEqual(product.image_large_url, "L")
        self.assertEqual(product.category_name, "Cards")

    def test_sha256_is_computed_from_raw_bytes(self):
        data = catalog_json(DOC)
        self.assertEqual(self.parse(data).sha256, hashlib.sha256(data).hexdigest())
        # The same document written differently is another file.
        other = json.dumps(DOC, indent=2).encode()
        self.assertNotEqual(self.parse(other).sha256, self.parse(data).sha256)

    def test_gzip_file_is_rejected(self):
        # The former format (gzip-compressed JSON lines) is not read any more.
        with self.assertRaises(CatalogFilesError):
            self.parse(b"\x1f\x8b\x08\x00not json")

    def test_invalid_json_is_rejected(self):
        with self.assertRaises(CatalogFilesError):
            self.parse(b"{not json")

    def test_document_that_is_not_an_object_is_rejected(self):
        with self.assertRaises(CatalogFilesError):
            self.parse(b"[]")

    def test_missing_list_is_rejected(self):
        with self.assertRaises(CatalogFilesError):
            self.parse(catalog_json({"expansions": DOC["expansions"]}))

    def test_extra_keys_are_ignored(self):
        game = self.parse(catalog_json({**DOC, "future": [1]}))
        self.assertEqual(len(game.expansions), 2)
        self.assertEqual(len(game.products), 1)

    def test_missing_field_is_rejected(self):
        with self.assertRaises(CatalogFilesError):
            self.parse(catalog_json({"expansions": [], "products": [{"id_product": 1}]}))

    def test_missing_bucket_setting_raises(self):
        with override_settings(CARDLEDGER_CATALOG_BUCKET=""):
            with self.assertRaisesMessage(
                CatalogFilesError, "CARDLEDGER_CATALOG_BUCKET is not set"
            ):
                CatalogBucket()

    def test_empty_region_defaults_to_us_east_1(self):
        # An empty region would make boto3 build "https://s3..amazonaws.com"; AWS_* variables are never read.
        env = {"AWS_DEFAULT_REGION": "eu-central-1", "AWS_CONFIG_FILE": "/nonexistent"}
        with mock.patch.dict(os.environ, env), override_settings(
            CARDLEDGER_CATALOG_ENDPOINT="", CARDLEDGER_CATALOG_REGION=""
        ):
            client = CatalogBucket().client
        self.assertEqual(client.meta.region_name, "us-east-1")
        self.assertEqual(client.meta.endpoint_url, "https://s3.amazonaws.com")

    def test_configured_region_is_used(self):
        env = {"AWS_DEFAULT_REGION": "us-west-2"}
        with mock.patch.dict(os.environ, env), override_settings(
            CARDLEDGER_CATALOG_ENDPOINT="", CARDLEDGER_CATALOG_REGION="eu-west-1"
        ):
            client = CatalogBucket().client
        self.assertEqual(client.meta.region_name, "eu-west-1")

    def test_credentials_come_from_cardledger_settings(self):
        env = {"AWS_ACCESS_KEY_ID": "aws-key", "AWS_SECRET_ACCESS_KEY": "aws-secret"}
        with mock.patch.dict(os.environ, env), override_settings(
            CARDLEDGER_CATALOG_ACCESS_KEY_ID="cl-key", CARDLEDGER_CATALOG_SECRET_ACCESS_KEY="cl-secret",
            CARDLEDGER_CATALOG_REGION="auto", CARDLEDGER_CATALOG_ENDPOINT="https://example.r2.cloudflarestorage.com",
        ):
            client = CatalogBucket().client
        credentials = client._request_signer._credentials
        self.assertEqual((credentials.access_key, credentials.secret_key), ("cl-key", "cl-secret"))
        self.assertEqual(client.meta.endpoint_url, "https://example.r2.cloudflarestorage.com")
