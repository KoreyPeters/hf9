from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

from .utils import generate_username


class AccountAdapter(DefaultAccountAdapter):
    def populate_username(self, request, user):
        if not user.username:
            user.username = generate_username()


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)
        if not user.username:
            user.username = generate_username()
        if not user.display_name and data.get("name"):
            user.display_name = data["name"]
        return user
