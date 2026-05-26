from allauth.socialaccount.signals import social_account_added
from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver
from django.http import HttpRequest
from django.utils import timezone


@receiver(social_account_added)
def mark_social_player_verified(sender, request, sociallogin, **kwargs):
    player = sociallogin.user
    if not player.email_verified:
        player.email_verified = True
        player.email_verified_at = timezone.now()
        player.save(update_fields=["email_verified", "email_verified_at"])


@receiver(user_logged_in)
def auto_verify_on_magic_link(sender, request: HttpRequest, user, **kwargs) -> None:
    backend = getattr(user, "backend", "")
    if backend == "sesame.backends.ModelBackend" and not user.email_verified:
        user.email_verified = True
        user.email_verified_at = timezone.now()
        user.save(update_fields=["email_verified", "email_verified_at"])
