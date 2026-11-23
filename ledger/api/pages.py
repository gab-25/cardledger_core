"""Paths of the React UI's pages that the API hands to the client (links and redirects)."""

COLLECTION = "/"


def collection(game):
    """The collection page of a game."""
    return f"/{game.slug}/"


def scan_camera(session):
    """The camera page of a scan session, under its game."""
    return f"/{session.game.slug}/scan/{session.uuid}/"
