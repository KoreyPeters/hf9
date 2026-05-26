# Uvicorn Migration Plan

Replace Daphne with Uvicorn as the ASGI server. Goal: auto-reload on file change in
development, no change to production behaviour.

---

## Current State

| Item | Value |
|---|---|
| Server | `daphne>=4.2.1` (main dependency) |
| Dev start command | `daphne hf.asgi:application` (or similar) |
| `hf/asgi.py` | Standard `get_asgi_application()` — no Channels routing |
| `INSTALLED_APPS` | Daphne is **not** listed — no Django integration to undo |
| `ASGI_APPLICATION` | `"hf.asgi.application"` — unchanged |

Daphne is Django Channels' reference server. This project does not use Channels, so Daphne
provides no advantage over a general-purpose ASGI server.

---

## What Does Not Change

- `hf/asgi.py` — unchanged
- `ASGI_APPLICATION` setting — unchanged
- `hf/settings/prod.py` — unchanged
- All other Django settings

---

## Changes

### §1 — Swap dependencies in `pyproject.toml` ✅ COMPLETE

Remove `daphne` from main dependencies. Add `uvicorn` to main dependencies (prod needs it),
and `watchfiles` to dev dependencies (powers `--reload`).

```toml
# pyproject.toml

[project]
dependencies = [
    # remove:  "daphne>=4.2.1",
    "uvicorn>=0.34.0",
    ...
]

[dependency-groups]
dev = [
    "watchfiles>=1.0.0",   # add — enables --reload
    ...
]
```

`watchfiles` is kept in dev deps because `--reload` is only used in development. Uvicorn's
`[standard]` extras bundle it, but that also pulls in `uvloop` which does not support Windows;
separating them keeps the install clean on all platforms.

---

### §2 — Dev startup command

Replace the Daphne invocation with:

```
uvicorn hf.asgi:application --reload --port 8000
```

`--reload` watches for `.py`, `.html`, `.css`, `.js` file changes and restarts automatically.
If you use a PyCharm run configuration, update it to this command.

To also reload on template changes (`.html`), pass explicit watch paths:

```
uvicorn hf.asgi:application --reload --reload-include "*.html" --port 8000
```

---

### §3 — Production startup command

The production command (wherever it is defined — Cloud Run service yaml, Procfile, or shell
script) changes from:

```
daphne -b 0.0.0.0 -p 8080 hf.asgi:application
```

to:

```
uvicorn hf.asgi:application --host 0.0.0.0 --port 8080 --workers 1
```

`--workers 1` is correct for Cloud Run — horizontal scaling is handled by adding container
instances, not by forking workers inside one container. If you later deploy on a VM or use
Gunicorn for multi-worker support, the command becomes:

```
gunicorn hf.asgi:application -k uvicorn.workers.UvicornWorker -w 2 -b 0.0.0.0:8080
```

---

## Sequencing

1. ✅ Update `pyproject.toml` — swap `daphne` → `uvicorn`, add `watchfiles` to dev group
2. Run `uv sync` — requires stopping any running server first (Windows file lock on daphne `.pyd`)
3. Start the server with `uvicorn hf.asgi:application --reload --reload-include "*.html" --port 8000`
4. Edit a `.py` file and confirm the server reloads automatically
5. Update the production startup command wherever it is defined

---

## No Migration Required

- No database changes
- No Django settings changes
- No changes to `asgi.py`, URL config, or any app code
- Tests are unaffected (pytest never starts a server)
