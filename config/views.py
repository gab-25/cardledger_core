import mimetypes

from django.conf import settings
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404, HttpResponse
from django.views.decorators.csrf import ensure_csrf_cookie


@ensure_csrf_cookie
def ui_app(request):
    """The React UI (gab-25/cardledger_ui): every page URL serves its index.html; the app routes on the client.

    No login here: the UI asks /api/me/ and goes to its login page when nobody is logged in.
    """
    index = settings.UI_APP_DIR / "index.html"
    try:
        html = index.read_bytes()
    except OSError:
        return HttpResponse("UI not built: set UI_APP_DIR (see README)", status=503,
                            content_type="text/plain; charset=utf-8")
    return HttpResponse(html, content_type="text/html; charset=utf-8")


def media(request, path):
    """An uploaded file (a purchase lot attachment) from the default storage; the names are random, the files public."""
    if ".." in path.split("/") or not default_storage.exists(path):
        raise Http404()
    content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    response = FileResponse(default_storage.open(path), content_type=content_type)
    response["X-Content-Type-Options"] = "nosniff"
    return response
