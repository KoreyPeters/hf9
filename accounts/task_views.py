from datetime import timedelta

from django.core.mail import send_mail
from django.utils import timezone

from core.tasks import enqueue, task


@task("verify-email-reminder")
def send_verification_reminder(player_id: int) -> None:
    from .models import Player

    try:
        player = Player.objects.get(pk=player_id)
    except Player.DoesNotExist:
        return

    if player.email_verified:
        return

    if timezone.now() - player.date_joined > timedelta(days=30):
        return

    send_mail(
        subject="Reminder: verify your Human Flourishing email",
        message=(
            "You haven't verified your email address yet.\n\n"
            "Without verification you can browse and survey, but you won't earn points.\n\n"
            "Log in to Human Flourishing and click the verification link in your inbox, "
            "or request a new one from inside the app."
        ),
        from_email="noreply@humanflourishing.org",
        recipient_list=[player.email],
    )

    enqueue(
        "verify-email-reminder",
        {"player_id": player_id},
        schedule_time=timezone.now() + timedelta(days=7),
    )
