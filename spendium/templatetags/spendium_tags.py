"""Template tags for the shared layout.

One tag so far, and it exists because `base.html` is shared with Polium. The
alternative — a context processor — runs on every page in both games, and the
question this answers costs database queries to ask.
"""

from django import template
from django.http import HttpRequest

from .. import service, spending

register = template.Library()


@register.inclusion_tag("spendium/partials/upload_fab.html", takes_context=True)
def upload_fab(context: dict) -> dict:
    """The shortcut to photographing a receipt, or nothing.

    Rendered only where it would work. `receipt_upload` answers a player who may
    not upload with a 403 page, which is a fair reply to a deliberate visit and a
    poor one to a button that looked available — so the same gate has to be
    asked here, and `service.may_upload` is where both callers get it from.

    Hidden entirely during an emergency stop. `accept_upload` would refuse the
    receipt anyway, so leaving the button up would spend the player's attention
    and their photo to deliver a message they did not need. The upload page still
    exists and still explains itself for anyone who goes looking.

    Costs two queries where it renders and none anywhere else, which is the whole
    reason this is a tag called from inside a namespace guard rather than a
    context processor.
    """
    request: HttpRequest | None = context.get("request")
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"show": False}
    if spending.uploads_paused() or not service.may_upload(user):
        return {"show": False}
    return {"show": True}
