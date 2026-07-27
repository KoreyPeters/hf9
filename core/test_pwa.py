"""Installability.

The manifest shipped broken for as long as it existed — it pointed at icons that
were never created, at a path production does not serve — and nothing failed,
because a manifest nobody can install is indistinguishable from a working one
until somebody tries to install it. These are the assertions that would have
caught that.
"""

import json
from pathlib import Path

import pytest
from django.conf import settings
from PIL import Image

ICON_DIR = Path(settings.BASE_DIR) / "static" / "icons"


@pytest.fixture
def manifest(client) -> dict:
    response = client.get("/manifest.json")
    assert response.status_code == 200
    return json.loads(response.content)


# ── The icons exist and are usable ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "size"),
    [
        ("icon-192.png", 192),
        ("icon-512.png", 512),
        ("icon-maskable-512.png", 512),
        ("apple-touch-icon.png", 180),
    ],
)
def test_the_icon_exists_at_its_declared_size(name: str, size: int) -> None:
    path = ICON_DIR / name
    assert path.exists(), f"{name} is missing; run `manage.py generate_icons`"
    with Image.open(path) as image:
        assert image.size == (size, size)


@pytest.mark.parametrize(
    "name",
    ["icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"],
)
def test_the_icon_is_opaque(name: str) -> None:
    """iOS composites a transparent home-screen icon onto black, so an alpha
    channel here shows up as a dark ring rather than as nothing."""
    with Image.open(ICON_DIR / name) as image:
        assert image.mode == "RGB"


# ── The manifest points somewhere real ────────────────────────────────────────


def test_icon_urls_follow_static_url(client, settings) -> None:
    """The exact bug this replaces, and the only test here that reproduces it.

    Asserting against `staticfiles_storage.url()` under dev settings proves
    nothing: STATIC_URL is `/static/` locally, so a hardcoded `/static/icons/...`
    and a properly built URL are the same string. The failure only appears when
    STATIC_URL points somewhere else — which is exactly the production case, where
    it is a GCS bucket and `/static/` is served by nothing.

    So move STATIC_URL and require the manifest to follow it.
    """
    settings.STATIC_URL = "https://cdn.example.test/s/"

    payload = json.loads(client.get("/manifest.json").content)
    sources = [icon["src"] for icon in payload["icons"]]

    assert sources, "the manifest declares no icons at all"
    for src in sources:
        assert src.startswith("https://cdn.example.test/s/"), (
            f"{src} ignores STATIC_URL, so it will 404 wherever static files are "
            "not served from the same origin"
        )


def test_every_declared_icon_is_a_file_that_exists(manifest: dict) -> None:
    """Ties the manifest to the tree. Renaming an icon without updating the view
    otherwise breaks installability silently."""
    for icon in manifest["icons"]:
        name = icon["src"].rstrip("/").split("/")[-1]
        assert (ICON_DIR / name).exists(), f"{name} is declared but not present"


def test_chromium_has_the_sizes_it_requires(manifest: dict) -> None:
    """192 and 512 are the documented minimum for an install prompt."""
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert "192x192" in sizes
    assert "512x512" in sizes


def test_a_maskable_icon_is_offered(manifest: dict) -> None:
    """Without one, Android crops the `any` icon to its own shape and cuts the
    mark."""
    assert any(icon.get("purpose") == "maskable" for icon in manifest["icons"])


def test_it_declares_a_scope_and_a_stable_id(manifest: dict) -> None:
    assert manifest["scope"] == "/"
    assert manifest["id"]
    assert manifest["display"] == "standalone"


def test_it_is_served_as_a_manifest(client) -> None:
    response = client.get("/manifest.json")
    assert response["Content-Type"] == "application/manifest+json"


# ── iOS reads none of the above ───────────────────────────────────────────────


def test_the_page_offers_an_apple_touch_icon(client) -> None:
    """Safari ignores the manifest's icons. Without this an installed app takes a
    screenshot of the page as its home screen icon."""
    body = client.get("/").content.decode()
    assert 'rel="apple-touch-icon"' in body
    assert "apple-touch-icon.png" in body


def test_the_page_asks_ios_for_standalone(client) -> None:
    body = client.get("/").content.decode()
    assert 'name="apple-mobile-web-app-capable"' in body
    assert 'name="mobile-web-app-capable"' in body


def test_the_status_bar_style_suits_a_light_nav(client) -> None:
    """`black-translucent` would slide the page up underneath the clock."""
    body = client.get("/").content.decode()
    assert 'name="apple-mobile-web-app-status-bar-style" content="default"' in body


def test_the_theme_colour_matches_the_manifest(client, manifest: dict) -> None:
    """They tint the same surface on different platforms; disagreeing means the
    status bar changes colour depending on how the app was installed."""
    body = client.get("/").content.decode()
    assert f'name="theme-color" content="{manifest["theme_color"]}"' in body
