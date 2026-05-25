from django.http import HttpRequest, JsonResponse


def manifest(request: HttpRequest) -> JsonResponse:
    return JsonResponse({
        "name": "Human Flourishing",
        "short_name": "HF",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#000000",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    })
