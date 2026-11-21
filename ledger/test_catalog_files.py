import gzip
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

from ledger.catalog_files import CatalogBucket, CatalogFilesError

BUCKET = "test-bucket"
LINES = [
    {"type": "expansion", "code": "1", "name": "First", "released_at": "2023-09-01", "language": ""},
    {"type": "expansion", "code": "2", "name": "Second", "released_at": None, "language": "en"},
    {"type": "product", "id_product": 10, "name": "Elsa", "expansion_code": "1", "number": "5",
     "rarity": "Rare", "image_url": "u", "image_large_url": "L", "category_id": 1, "category_name": "Cards"},
]


def jsonl_gz(lines):
    return gzip.compress("\n".join(json.dumps(x) for x in lines).encode() + b"\n")


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

    def entry(self, data, sha=None):
        return {"key": "catalog/v1/lorcana.jsonl.gz", "sha256": sha or hashlib.sha256(data).hexdigest()}

    def test_manifest_is_read(self):
        manifest = {"format_version": 1, "games": {"mtg": {"key": "k"}}}
        self.expect_get("catalog/v1/manifest.json", json.dumps(manifest).encode())
        self.assertEqual(CatalogBucket(client=self.client).manifest(), manifest)

    def test_unknown_format_version_is_rejected(self):
        self.expect_get("catalog/v1/manifest.json", b'{"format_version": 2, "games": {}}')
        with self.assertRaises(CatalogFilesError):
            CatalogBucket(client=self.client).manifest()

    def test_game_file_is_parsed(self):
        data = jsonl_gz(LINES)
        self.expect_get("catalog/v1/lorcana.jsonl.gz", data)
        game = CatalogBucket(client=self.client).game_file("lorcana", self.entry(data))
        self.assertEqual(game.slug, "lorcana")
        self.assertEqual(game.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual([e.code for e in game.expansions], ["1", "2"])
        self.assertEqual(game.expansions[0].released_at, date(2023, 9, 1))
        self.assertIsNone(game.expansions[1].released_at)
        self.assertEqual(game.expansions[1].language, "en")
        (product,) = game.products
        self.assertEqual(product.id_product, 10)
        self.assertEqual(product.expansion_code, "1")
        self.assertEqual(product.image_large_url, "L")
        self.assertEqual(product.category_name, "Cards")

    def test_sha256_mismatch_is_rejected(self):
        data = jsonl_gz(LINES)
        self.expect_get("catalog/v1/lorcana.jsonl.gz", data)
        with self.assertRaises(CatalogFilesError):
            CatalogBucket(client=self.client).game_file("lorcana", self.entry(data, sha="0" * 64))

    def test_corrupt_gzip_is_rejected(self):
        data = b"not gzip at all"
        self.expect_get("catalog/v1/lorcana.jsonl.gz", data)
        with self.assertRaises(CatalogFilesError):
            CatalogBucket(client=self.client).game_file("lorcana", self.entry(data))

    def test_unknown_line_type_is_ignored(self):
        data = jsonl_gz([{"type": "future", "x": 1}] + LINES)
        self.expect_get("catalog/v1/lorcana.jsonl.gz", data)
        game = CatalogBucket(client=self.client).game_file("lorcana", self.entry(data))
        self.assertEqual(len(game.expansions), 2)
        self.assertEqual(len(game.products), 1)

    def test_missing_field_is_rejected(self):
        data = jsonl_gz([{"type": "product", "id_product": 1}])
        self.expect_get("catalog/v1/lorcana.jsonl.gz", data)
        with self.assertRaises(CatalogFilesError):
            CatalogBucket(client=self.client).game_file("lorcana", self.entry(data))

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
