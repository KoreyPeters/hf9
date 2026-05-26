from django.core.cache import cache
from django.http import HttpRequest


def check_rate_limit(request: HttpRequest, action: str, limit: int = 10, window: int = 3600) -> bool:
    ip = request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR", "unknown"))
    ip = ip.split(",")[0].strip()
    key = f"rl:{action}:{ip}"
    count = cache.get(key, 0)
    if count >= limit:
        return False
    cache.set(key, count + 1, window)
    return True
