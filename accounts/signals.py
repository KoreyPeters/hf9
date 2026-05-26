from allauth.socialaccount.signals import social_account_added
from django.dispatch import receiver
from django.utils import timezone


@receiver(social_account_added)
def mark_social_player_verified(sender, request, sociallogin, **kwargs):
    player = sociallogin.user
    if not player.email_verified:
        player.email_verified = True
        player.email_verified_at = timezone.now()
        player.save(update_fields=["email_verified", "email_verified_at"])
