"""Storage locations (/api/locations/): the user's boxes and binders with their capacity, the copies in each (items
whose location equals the code, every game) and a suggestion of where to put new copies."""

from django.db import transaction
from django.db.models import Sum
from django.db.models.functions import Now
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from ..forms import LocationForm
from ..models import CollectionItem, Location
from .common import doc, form_body, form_errors, messages_response, msg

TAGS = ["locations"]
LOCATION = form_body("Location", LocationForm)


def used_by_code(user):
    """{location code: copies of the user's items there} (every game; blank locations left out)."""
    rows = (CollectionItem.objects.owned_by(user).exclude(location="").values("location")
            .annotate(n=Sum("quantity")).order_by())
    return {r["location"]: r["n"] or 0 for r in rows}


def location_rows(user):
    """The user's locations, then the codes used by items without a Location row (id and capacity null), by code."""
    used = used_by_code(user)
    rows = []
    for loc in Location.objects.owned_by(user):
        n = used.pop(loc.code, 0)
        rows.append({"id": loc.pk, "code": loc.code, "capacity": loc.capacity, "used": n,
                     "free": None if loc.capacity is None else loc.capacity - n, "note": loc.note})
    rows += [{"id": None, "code": code, "capacity": None, "used": n, "free": None, "note": ""}
             for code, n in used.items()]
    return sorted(rows, key=lambda r: r["code"])


@doc(TAGS, "The user's storage locations (GET: {results: [{id, code, capacity, used, free, note}]}: used = copies of "
           "the items whose location is the code, every game; free = capacity − used, negative when over capacity, "
           "null without a capacity; codes written on items without a location row come with id and capacity null), "
           "or add one (POST: code, unique per user, capacity, note; answers its id)", request=LOCATION)
@api_view(["GET", "POST"])
def locations(request):
    if request.method == "GET":
        return Response({"results": location_rows(request.user)})
    form = LocationForm(request.data, instance=Location(user=request.user))
    if not form.is_valid():
        raise form_errors(form)
    loc = form.save()
    return messages_response(msg("success", f"Location {loc.code} added."), status=status.HTTP_201_CREATED, id=loc.pk)


@doc(TAGS, "Change a location (PATCH: a field left out keeps its value; a new code also moves the items written with "
           "the old one) or delete it (DELETE: the items keep their location text)", request=LOCATION)
@api_view(["PATCH", "DELETE"])
def location(request, pk):
    obj = get_object_or_404(Location.objects.owned_by(request.user), pk=pk)
    if request.method == "DELETE":
        obj.delete()
        return messages_response(msg("success", f"Location {obj.code} deleted."))
    old = obj.code
    form = LocationForm(request.data, instance=obj)
    if not form.is_valid():
        raise form_errors(form)
    with transaction.atomic():
        loc = form.save()
        if loc.code != old:
            CollectionItem.objects.owned_by(request.user).filter(location=old).update(location=loc.code,
                                                                                    updated_at=Now())
    return messages_response(msg("success", f"Location {loc.code} saved."))


@doc(TAGS, "Where to put ?copies new copies (default 1): the location with a capacity that has the most free slots, "
           "at least copies (ties: by code); {code: null} when none has room", parameters=[
    OpenApiParameter("copies", OpenApiTypes.INT, OpenApiParameter.QUERY, description="Copies to store (default 1)"),
])
@api_view(["GET"])
def suggest(request):
    raw = request.query_params.get("copies", "1")
    try:
        copies = int(raw)
    except ValueError:
        raise ValidationError({"copies": ["A whole number."]}) from None
    if copies < 1:
        raise ValidationError({"copies": ["At least 1."]})
    room = [r for r in location_rows(request.user) if r["free"] is not None and r["free"] >= copies]
    best = min(room, key=lambda r: (-r["free"], r["code"]), default=None)
    return Response({"code": best["code"] if best else None})
