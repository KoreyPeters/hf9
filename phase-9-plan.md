# Phase 9 Todo — Frontend: Datastar + PWA

Phase 9 produces the shared frontend infrastructure: the SSE utility module, the base HTML template that loads Datastar and declares the PWA manifest, the manifest view, and the static icon placeholder directory. No game-specific views or templates are built here — those belong to the app phases.

---

#### 9.1 — Settings: STATICFILES_DIRS (hf/settings/base.py)

- [x] Add `STATICFILES_DIRS = [BASE_DIR / "static"]` to `hf/settings/base.py`
  - `TEMPLATES` is already configured with `DIRS: [BASE_DIR / "templates"]` — no change needed there
  - `STATIC_URL = "static/"` is already set — no change needed
  - `STATICFILES_DIRS` is the missing piece: without it, the top-level `static/` directory is invisible to Django's `staticfiles` machinery in both `runserver` and `collectstatic`
  - Place it adjacent to `STATIC_URL`

---

#### 9.2 — SSE infrastructure (core/sse.py)

- [x] Create `core/sse.py`
- [x] Add imports:
  - `import asyncio`
  - `import json`
  - `from collections.abc import AsyncGenerator, Generator`
  - `from typing import Any`
  - `from django.http import StreamingHttpResponse`
- [x] Write `sse_response(generator: Generator[tuple[str, Any], None, None]) -> StreamingHttpResponse`:
  - Define inner generator `stream()` that iterates the argument:
    - For each `(event, data)` tuple: `yield f"event: {event}\n"` then `yield f"data: {json.dumps(data)}\n\n"`
  - Build `response = StreamingHttpResponse(stream(), content_type="text/event-stream")`
  - Set `response["Cache-Control"] = "no-cache"` — prevents proxies from buffering
  - Set `response["X-Accel-Buffering"] = "no"` — disables nginx proxy buffering
  - Return `response`
- [x] Write `keepalive_generator(real_generator: AsyncGenerator[str, None]) -> AsyncGenerator[str, None]` as an `async def` function:
  - `async for event in real_generator: yield event` — proxy all events from the real generator
  - `while True: await asyncio.sleep(25); yield ": keepalive\n\n"` — after the real generator exhausts, emit a keepalive comment every 25 seconds to prevent the Cloud Run load balancer from closing idle connections
  - This is an async generator function — the `yield` inside an `async def` makes it one implicitly; no explicit return type annotation needed beyond the function signature

---

#### 9.3 — Base template (templates/base.html)

- [x] Create the top-level `templates/` directory (already on the search path via `TEMPLATES[0]["DIRS"]`)
- [x] Create `templates/base.html`:
  - `<!doctype html>`
  - `<html lang="en">`
  - `<head>`:
    - `<meta charset="UTF-8">`
    - `<meta name="viewport" content="width=device-width, initial-scale=1.0">`
    - `<title>{% block title %}Human Flourishing{% endblock %}</title>`
    - Datastar CDN: `<script type="module" src="https://cdn.jsdelivr.net/npm/@starfederation/datastar@latest/dist/datastar.js"></script>`
    - Manifest link: `<link rel="manifest" href="/manifest.json">`
  - `<body>`:
    - `{% block content %}{% endblock %}`
  - `</html>`

---

#### 9.4 — PWA manifest view (hf/views.py)

The manifest must be served at `/manifest.json` (not `/static/manifest.json`) because browsers expect the PWA manifest at an explicit path declared in `<link rel="manifest">`. A small Django view is the cleanest way to serve it without static-file path tricks.

- [x] Create `hf/views.py`
- [x] Add imports:
  - `from django.http import HttpRequest, JsonResponse`
- [x] Write `manifest(request: HttpRequest) -> JsonResponse`:
  - Return `JsonResponse` with the following dict:
    ```
    {
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
    }
    ```

---

#### 9.5 — Wire manifest URL (hf/urls.py)

- [x] Add `from hf import views as hf_views` to `hf/urls.py`
- [x] Add `path("manifest.json", hf_views.manifest, name="manifest")` to `urlpatterns` — place it before the `include()` entries so it matches before any app catches it

---

#### 9.6 — Static icon placeholder

- [x] Create `static/icons/` directory
- [x] Add a `.gitkeep` file inside `static/icons/` so the directory is tracked by git — actual 192×192 and 512×512 PNG icons are out of scope for Phase 9 and will be added before first deployment
- [x] Run `uv run python manage.py check` — must be clean

---

#### Phase 9 complete when
- [x] `hf/settings/base.py` includes `STATICFILES_DIRS = [BASE_DIR / "static"]`
- [x] `core/sse.py` defines `sse_response` (synchronous, returns `StreamingHttpResponse`) and `keepalive_generator` (async generator, 25-second keepalive interval)
- [x] `templates/base.html` exists with Datastar CDN script, manifest link, and `title`/`content` blocks
- [x] `hf/views.py` defines `manifest(request) -> JsonResponse` returning the full PWA manifest dict
- [x] `hf/urls.py` serves `manifest.json` at `/manifest.json` via the named URL `manifest`
- [x] `static/icons/` directory exists with `.gitkeep`
- [x] `uv run python manage.py check` → `System check identified no issues`
