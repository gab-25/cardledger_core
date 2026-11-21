"""Reads the catalog files (format v1) that cardledger_database generates, from S3: one file per game, no manifest."""

import gzip
import hashlib
import json
from dataclasses import dataclass
from datetime import date

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings

FORMAT_VERSION = 1


class CatalogFilesError(Exception):
    pass


class CatalogFileMissing(CatalogFilesError):
    """The bucket holds no file for that game."""


def catalog_key(slug):
    """Where a game's file is in the bucket, the same key the generator uploads it to."""
    return f"catalog/v{FORMAT_VERSION}/{slug}.jsonl.gz"


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
    sha256: str  # of the file's raw (compressed) bytes
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


def _s3_client():
    """An S3 client configured only from the CARDLEDGER_CATALOG_* settings (the AWS_* variables are never used).

    An empty region falls back to us-east-1: boto3 would otherwise build "https://s3..amazonaws.com" from it.
    """
    return boto3.client(
        "s3",
        endpoint_url=getattr(settings, "CARDLEDGER_CATALOG_ENDPOINT", "") or None,
        region_name=getattr(settings, "CARDLEDGER_CATALOG_REGION", "") or "us-east-1",
        aws_access_key_id=getattr(settings, "CARDLEDGER_CATALOG_ACCESS_KEY_ID", "") or None,
        aws_secret_access_key=getattr(settings, "CARDLEDGER_CATALOG_SECRET_ACCESS_KEY", "") or None,
    )


class CatalogBucket:
    def __init__(self, client=None):
        self.bucket = getattr(settings, "CARDLEDGER_CATALOG_BUCKET", "")
        if not self.bucket:
            raise CatalogFilesError("CARDLEDGER_CATALOG_BUCKET is not set")
        if client is None:
            client = _s3_client()
        self.client = client

    def fetch(self, slug):
        """The raw bytes of slug's file. Raises CatalogFileMissing when the bucket has none."""
        key = catalog_key(slug)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise CatalogFileMissing(f"No catalog file {key}") from exc
            raise CatalogFilesError(f"Cannot read {key}: {exc}") from exc
        except BotoCoreError as exc:
            raise CatalogFilesError(f"Cannot read {key}: {exc}") from exc

    def parse(self, slug, data):
        """slug's file from its raw bytes (expansion lines, then product lines; other lines are ignored)."""
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
        except (OSError, EOFError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise CatalogFilesError(f"Invalid catalog file {catalog_key(slug)}: {exc}") from exc
        return GameFile(slug, hashlib.sha256(data).hexdigest(), expansions, products)
