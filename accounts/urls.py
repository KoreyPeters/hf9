from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_page, name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("login/magic/request/", views.magic_link_request, name="magic_link_request"),
    path("login/magic/", views.MagicLinkVerifyView.as_view(), name="magic_link_verify"),
    path("signup/", views.signup, name="signup"),
    path("welcome/", views.welcome, name="welcome"),
    path("verify-email/resend/", views.resend_verification, name="resend_verification"),
    path("verify-email/<str:token>/", views.verify_email, name="verify_email"),
    path("passkey/register/options/", views.passkey_register_options, name="passkey_register_options"),
    path("passkey/register/verify/", views.passkey_register_verify, name="passkey_register_verify"),
    path("passkey/auth/options/", views.passkey_auth_options, name="passkey_auth_options"),
    path("passkey/auth/verify/", views.passkey_auth_verify, name="passkey_auth_verify"),
]
