"""Reads the catalog files (format v1) that cardledger_database generates, from S3."""

import gzip
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings

FORMAT_VERSION = 1
MANIFEST_KEY = "catalog/v1/manifest.json"


class CatalogFilesError(Exception):
    pass


@dataclass(frozen=True)
class FileExpansion:
    code: str
    name: str
    released_at: date | None
    language: str


@dataclass(frozen=True)
class FileProduct:
    id_product: int
    name: str
    expansion_code: str
    number: str
    rarity: str
    image_url: str
    image_large_url: str
    category_id: int
    category_name: str


@dataclass(frozen=True)
class GameFile:
    slug: str
    sha256: str
    generated_at: datetime | None
    expansions: list[FileExpansion]
    products: list[FileProduct]


def _expansion(row):
    released = row["released_at"]
    return FileExpansion(
        code=row["code"],
        name=row["name"],
        released_at=date.fromisoformat(released) if released else None,
        language=row["language"],
    )


def _product(row):
    return FileProduct(
        id_product=int(row["id_product"]),
        name=row["name"],
        expansion_code=row["expansion_code"],
        number=row["number"],
        rarity=row["rarity"],
        image_url=row["image_url"],
        image_large_url=row["image_large_url"],
        category_id=int(row["category_id"]),
        category_name=row["category_name"],
    )


class CatalogBucket:
    def __init__(self, client=None):
        self.bucket = getattr(settings, "CARDLEDGER_CATALOG_BUCKET", "")
        if not self.bucket:
            raise CatalogFilesError("CARDLEDGER_CATALOG_BUCKET is not set")
        if client is None:
            endpoint = getattr(settings, "CARDLEDGER_CATALOG_ENDPOINT", "") or None
            client = boto3.client("s3", endpoint_url=endpoint)
        self.client = client

    def _read(self, key):
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except (BotoCoreError, ClientError) as exc:
            raise CatalogFilesError(f"Cannot read {key}: {exc}") from exc

    def manifest(self):
        try:
            manifest = json.loads(self._read(MANIFEST_KEY))
        except ValueError as exc:
            raise CatalogFilesError(f"Invalid manifest: {exc}") from exc
        if not isinstance(manifest, dict) or manifest.get("format_version") != FORMAT_VERSION:
            raise CatalogFilesError("Unsupported catalog format version")
        return manifest

    def game_file(self, slug, entry):
        try:
            key, expected = entry["key"], entry["sha256"]
        except (KeyError, TypeError) as exc:
            raise CatalogFilesError(f"Invalid manifest entry for {slug}") from exc
        data = self._read(key)
        if hashlib.sha256(data).hexdigest() != expected:
            raise CatalogFilesError(f"Checksum mismatch for {key}")
        expansions, products = [], []
        try:
            for line in gzip.decompress(data).decode("utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                kind = row.get("type")
                if kind == "expansion":
                    expansions.append(_expansion(row))
                elif kind == "product":
                    products.append(_product(row))
            generated_at = entry.get("generated_at")
            generated_at = datetime.fromisoformat(generated_at) if generated_at else None
        except (OSError, EOFError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise CatalogFilesError(f"Invalid catalog file {key}: {exc}") from exc
        return GameFile(slug, expected, generated_at, expansions, products)
