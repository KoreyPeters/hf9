"""Navbar badge.

Runs on every page render for a signed-in player, so it is cached rather than
recomputed — counting new items touches several tables, and the navbar is not
worth that on a page about something else entirely.

The cache is invalidated when the player visits the Action Centre, because the
one moment the number must be right is the moment it changes to zero.
"""

from django.core.cache import cache
from django.http import HttpRequest

BADGE_CACHE_SECONDS = 120


def cache_key(player_id: int) -> str:
    return f"spendium:action-badge:{player_id}"


def invalidate(player_id: int) -> None:
    cache.delete(cache_key(player_id))


def action_centre_badge(request: HttpRequest) -> dict[str, int]:
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"spendium_new_actions": 0}

    key = cache_key(user.pk)
    count = cache.get(key)
    if count is None:
        from . import action_centre

        count = action_centre.new_item_count(user)
        cache.set(key, count, BADGE_CACHE_SECONDS)
    return {"spendium_new_actions": count}
