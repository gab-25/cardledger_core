"""The vision index of each game (cardledger_vision), loaded from the database and kept per process."""

import cv2
from cardledger_vision import Index

from .models import GameVisionIndex

# One OpenCV thread per process: each container runs several gunicorn workers, which would otherwise all spawn a
# thread per core.
cv2.setNumThreads(1)

_cache = {}  # game.pk -> (sha256, Index)


def index_for(game):
    """The loaded vision index of `game`, or None when it has none. Only the sha256 is read on each call; the
    bytes are read and parsed again when it differs from the one cached (the sync stored a new index)."""
    sha = GameVisionIndex.objects.filter(game=game).values_list("sha256", flat=True).first()
    if sha is None:
        _cache.pop(game.pk, None)
        return None
    cached = _cache.get(game.pk)
    if cached is not None and cached[0] == sha:
        return cached[1]
    row = GameVisionIndex.objects.filter(game=game).values_list("sha256", "data").first()
    if row is None:  # deleted in between
        _cache.pop(game.pk, None)
        return None
    sha, data = row  # the sha of the bytes read, whatever changed since the first query
    index = Index.load(bytes(data))
    _cache[game.pk] = (sha, index)
    return index
