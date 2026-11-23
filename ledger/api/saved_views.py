"""Saved collection views (/api/collection/views/): named collection query strings, per user and game, in order."""

from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from ..forms import SavedViewForm
from ..models import SavedView
from .common import GAME, doc, form_body, form_errors, game_param, messages_response, msg

TAGS = ["collection"]
SAVED_VIEW = form_body("SavedView", SavedViewForm)


def _row(view):
    return {"id": view.pk, "name": view.name, "query": view.query, "position": view.position,
            "created_at": view.created_at}


def _move(view, position):
    """Put view at position (1-based, clamped) among the user's views of its game, renumbering the others."""
    others = list(SavedView.objects.owned_by(view.user).filter(game=view.game).exclude(pk=view.pk)
                  .order_by("position", "id"))
    position = len(others) + 1 if position is None else min(max(position, 1), len(others) + 1)
    others.insert(position - 1, view)
    for n, v in enumerate(others, start=1):
        if v.pk == view.pk:
            view.position = n
        elif v.position != n:
            SavedView.objects.filter(pk=v.pk).update(position=n)


@doc(TAGS, "The user's saved collection views of the game of ?game, in order (GET: {results: [{id, name, query, "
           "position, created_at}]}), or save one (POST: name, query = the collection query string, page and game "
           "dropped; position, default last; answers its id)", request=SAVED_VIEW, parameters=[GAME])
@extend_schema(methods=["GET"], operation_id="collection_views_list")
@api_view(["GET", "POST"])
def views(request):
    game = game_param(request.query_params)
    mine = SavedView.objects.owned_by(request.user).filter(game=game)
    if request.method == "GET":
        return Response({"results": [_row(v) for v in mine.order_by("position", "id")]})
    form = SavedViewForm(request.data, instance=SavedView(user=request.user, game=game))
    if not form.is_valid():
        raise form_errors(form)
    with transaction.atomic():
        view = form.save(commit=False)
        _move(view, form.cleaned_data.get("position"))
        view.save()
    return messages_response(msg("success", f"View “{view.name}” saved."), status=status.HTTP_201_CREATED,
                             id=view.pk, view=_row(view))


@doc(TAGS, "Rename a saved view, change its query or move it (PATCH: a field left out keeps its value), or delete it "
           "(DELETE: the others are renumbered)", request=SAVED_VIEW)
@api_view(["PATCH", "DELETE"])
def view(request, pk):
    obj = get_object_or_404(SavedView.objects.owned_by(request.user), pk=pk)
    if request.method == "DELETE":
        with transaction.atomic():
            obj.delete()
            for n, v in enumerate(SavedView.objects.owned_by(request.user).filter(game=obj.game_id)
                                  .order_by("position", "id"), start=1):
                if v.position != n:
                    SavedView.objects.filter(pk=v.pk).update(position=n)
        return messages_response(msg("success", f"View “{obj.name}” deleted."))
    form = SavedViewForm(request.data, instance=obj)
    if not form.is_valid():
        raise form_errors(form)
    with transaction.atomic():
        view = form.save(commit=False)
        if form.cleaned_data.get("position") is not None:
            _move(view, form.cleaned_data["position"])
        view.save()
    return messages_response(msg("success", f"View “{view.name}” saved."), view=_row(view))
