from django.contrib.staticfiles.storage import staticfiles_storage
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render


def landing(request: HttpRequest) -> HttpResponse:
    return render(request, "landing.html")


def about(request: HttpRequest) -> HttpResponse:
    return render(request, "about.html")


def _icon(name: str, sizes: str, purpose: str) -> dict[str, str]:
    return {
        "src": staticfiles_storage.url(f"icons/{name}"),
        "sizes": sizes,
        "type": "image/png",
        "purpose": purpose,
    }


def manifest(request: HttpRequest) -> JsonResponse:
    """The web app manifest.

    A view rather than a static file so the icon sources can be built through
    staticfiles storage. Hardcoding `/static/icons/...` is what made this 404 for
    the whole time it existed: in production STATIC_URL is the GCS bucket and
    nothing serves `/static/` at all, so the icons were unreachable and no browser
    would offer to install the site.
    """
    return JsonResponse(
        {
            # A stable identity for the installed app, so changing start_url
            # later updates the existing install instead of creating a second one.
            "id": "/",
            "name": "Human Flourishing",
            "short_name": "HF",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#ffffff",
            # White to match the nav, which is white and sticky: in standalone the
            # status bar sits directly above it, and the previous black would have
            # put a dark band over the top of a light page.
            "theme_color": "#ffffff",
            "icons": [
                _icon("icon-192.png", "192x192", "any"),
                _icon("icon-512.png", "512x512", "any"),
                # Android crops to its own shape and ignores `any` when a
                # maskable icon is offered. Without this the mark gets cut.
                _icon("icon-maskable-512.png", "512x512", "maskable"),
            ],
        },
        content_type="application/manifest+json",
    )
