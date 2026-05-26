from django.core.mail import send_mail
from django.http import HttpRequest
from sesame.utils import get_query_string

from .models import Player


def send_magic_link(request: HttpRequest, player: Player) -> None:
    link = request.build_absolute_uri("/accounts/login/magic/") + get_query_string(player)
    send_mail(
        subject="Your Human Flourishing login link",
        message=f"Click to log in (link expires in 15 minutes):\n\n{link}",
        from_email="noreply@humanflourishing.org",
        recipient_list=[player.email],
    )
