import os
from collections.abc import Callable

import pytest
from django.test import Client
from playwright.sync_api import Browser, Page

from accounts.models import Player

# pytest-playwright installs an asyncio event loop; Django refuses sync DB calls
# when it detects one unless this flag is set.
os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"


@pytest.fixture
def make_logged_in_page(browser: Browser) -> Callable[[Player], Page]:
    """Factory: call with a Player to get a new browser page logged in as that player."""
    contexts = []

    def _make(player: Player) -> Page:
        context = browser.new_context()
        contexts.append(context)
        client = Client()
        client.force_login(player)
        session_cookie = client.cookies["sessionid"]
        context.add_cookies([
            {
                "name": "sessionid",
                "value": session_cookie.value,
                "domain": "localhost",
                "path": "/",
            }
        ])
        return context.new_page()

    yield _make

    for ctx in contexts:
        ctx.close()
