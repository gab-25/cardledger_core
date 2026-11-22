"""The games API (/api/games/): the games of the catalog, for the game selector."""

from rest_framework.decorators import api_view
from rest_framework.response import Response

from ..models import Game
from .common import doc


@doc(["games"], "The games of the catalog, for the game selector")
@api_view(["GET"])
def games(request):
    return Response([{"slug": g.slug, "name": g.name} for g in Game.objects.all()])
