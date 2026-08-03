import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.http import HttpRequest
from django.utils import timezone

from .models import EmailVerification, Player


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def send_verification_email(request: HttpRequest, player: Player) -> None:
    EmailVerification.objects.filter(player=player, verified_at__isnull=True).delete()
    raw = secrets.token_urlsafe(32)
    EmailVerification.objects.create(
        player=player,
        token_hash=_hash(raw),
        expires_at=timezone.now()
        + timedelta(hours=settings.EMAIL_VERIFICATION_TTL_HOURS),
    )
    link = request.build_absolute_uri(f"/accounts/verify-email/{raw}/")
    # `None` means DEFAULT_FROM_EMAIL. This used to hardcode
    # noreply@humanflourishing.org — a third domain, matching neither
    # DEFAULT_FROM_EMAIL nor MAILGUN_SENDER_DOMAIN, and one the project no
    # longer owns. Mail from a domain we cannot publish SPF or DKIM for fails
    # alignment at the receiver, so verification mail was being rejected or
    # spam-filed; and once the domain changes hands, its bounces and replies
    # belong to somebody else.
    send_mail(
        subject="Verify your Human Flourishing email",
        message=f"Click to verify your email address:\n\n{link}",
        from_email=None,
        recipient_list=[player.email],
    )


class VerificationError(Exception):
    pass


def verify_email_token(raw: str) -> Player:
    try:
        record = EmailVerification.objects.select_related("player").get(
            token_hash=_hash(raw)
        )
    except EmailVerification.DoesNotExist:
        raise VerificationError("Invalid token.")
    if record.verified_at is not None:
        raise VerificationError("Already verified.")
    if record.expires_at < timezone.now():
        raise VerificationError("Token expired.")
    record.verified_at = timezone.now()
    record.save(update_fields=["verified_at"])
    player = record.player
    player.email_verified = True
    player.email_verified_at = timezone.now()
    player.save(update_fields=["email_verified", "email_verified_at"])
    return player
