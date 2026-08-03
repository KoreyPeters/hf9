from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest


def client_ip(request: HttpRequest) -> str:
    """The address to bucket a rate limit on.

    This used to take `X-Forwarded-For.split(",")[0]` — the *leftmost* entry,
    which is the part of the header the client supplies. Proxies append on the
    right, so the leftmost value is whatever the caller decided to write there,
    and the rate limit could be defeated by sending a different one each time.

    Confirmed against production on 2026-08-02 rather than assumed: six signup
    posts carrying `X-Forwarded-For: 203.0.113.77` filled the bucket at five,
    and changing the value to `203.0.113.88` was let straight through. One
    header per request bought unlimited signups.

    Google's documented behaviour is that an existing value is preserved and
    appended to, producing `<existing-value>,<client-ip>,<load-balancer-ip>`.
    This deployment has no load balancer — `terraform/load_balancer.tf.disabled`
    — and requests arrive through Cloud Run's own front end, which appends a
    single entry: a production traceback with no client-supplied header shows
    `HTTP_X_FORWARDED_FOR` holding exactly one address. So the trustworthy value
    is the last one, and `TRUSTED_PROXY_HOPS` is what says so out loud, because
    enabling that load balancer would move it to second-to-last and nothing else
    would notice.

    Falls back to `REMOTE_ADDR` when there is no header at all, which is the
    development case. In production `REMOTE_ADDR` is a link-local address shared
    by every request, so it is a last resort rather than a source.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        hops: int = getattr(settings, "TRUSTED_PROXY_HOPS", 1)
        entries = [part.strip() for part in forwarded.split(",") if part.strip()]
        if entries:
            # Clamped, so a header with fewer entries than configured hops falls
            # back to the leftmost rather than raising. That is the spoofable
            # value, but a request shaped that way did not come through the
            # infrastructure this setting describes, and refusing to rate limit
            # at all would be worse.
            index = max(0, len(entries) - hops)
            return entries[index]

    return request.META.get("REMOTE_ADDR", "unknown")


def check_rate_limit(
    request: HttpRequest, action: str, limit: int = 10, window: int = 3600
) -> bool:
    key = f"rl:{action}:{client_ip(request)}"
    count = cache.get(key, 0)
    if count >= limit:
        return False
    cache.set(key, count + 1, window)
    return True
