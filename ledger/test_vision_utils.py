"""Helpers for tests that need a real vision index and a photo of a card (no network)."""

import cv2
import numpy as np
from cardledger_vision import Entry, Header, encode, hash_card
from cardledger_vision.phash import CARD_SIZE


def synthetic_card(seed):
    """A deterministic 488x680 BGR card: random coloured shapes inside a dark 6px border."""
    rng = np.random.default_rng(seed)
    w, h = CARD_SIZE
    card = np.full((h, w, 3), int(rng.integers(90, 200)), np.uint8)
    for _ in range(14):
        colour = tuple(int(c) for c in rng.integers(0, 256, 3))
        x, y = int(rng.integers(10, w - 60)), int(rng.integers(10, h - 60))
        if rng.random() < 0.5:
            cv2.rectangle(card, (x, y), (x + int(rng.integers(30, 200)), y + int(rng.integers(30, 250))), colour, -1)
        else:
            cv2.circle(card, (x, y), int(rng.integers(20, 120)), colour, -1)
    cv2.rectangle(card, (0, 0), (w - 1, h - 1), (20, 20, 20), 12)  # 6px border on each side after drawing
    return card


def photo_of_card(seed):
    """(card_png, photo_jpeg): the synthetic card of `seed` as PNG bytes, and a JPEG photo of it on a gradient
    background, so that the library's detection finds it."""
    card = synthetic_card(seed)
    ok, png = cv2.imencode(".png", card)
    assert ok
    bg_h, bg_w = 1200, 1000
    ramp = np.linspace(200, 235, bg_w, dtype=np.uint8)
    photo = np.repeat(np.tile(ramp, (bg_h, 1))[:, :, None], 3, axis=2)
    w, h = 600, 836
    x, y = (bg_w - w) // 2, (bg_h - h) // 2
    photo[y:y + h, x:x + w] = cv2.resize(card, (w, h), interpolation=cv2.INTER_AREA)
    ok, jpg = cv2.imencode(".jpg", photo)
    assert ok
    return png.tobytes(), jpg.tobytes()


def make_index(game, entries, **header):
    """The bytes of a vision index for `game`. entries: a list of (blueprint, card_png) where card_png is the
    PNG of `photo_of_card(seed)[0]`, or None for a printing without an image (it has no hash). Extra keyword
    arguments go to the `Header` (e.g. confirm_only=True)."""
    rows = []
    for bp, png in entries:
        digest = None
        if png is not None:
            digest = hash_card(cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR))
        rows.append(Entry(bp.cm_product_id, bp.name, bp.expansion.code, bp.collector_number, digest))
    return encode(Header(game=game.slug, built_at="2026-01-01T00:00:00Z", catalog_sha256="0" * 64, **header), rows)
