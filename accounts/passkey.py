import json

import webauthn
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from webauthn.helpers import (
    base64url_to_bytes,
    parse_authentication_credential_json,
    parse_registration_credential_json,
)
from webauthn.helpers.structs import PublicKeyCredentialDescriptor

from .models import PasskeyCredential, Player


def registration_options(player: Player) -> dict:
    existing = [
        PublicKeyCredentialDescriptor(id=bytes(c.credential_id))
        for c in player.passkeys.all()
    ]
    options = webauthn.generate_registration_options(
        rp_id=settings.WEBAUTHN_RP_ID,
        rp_name=settings.WEBAUTHN_RP_NAME,
        user_id=str(player.pk).encode(),
        user_name=player.email,
        user_display_name=player.display_name or player.email,
        exclude_credentials=existing,
    )
    cache.set(f"webauthn_reg:{player.pk}", options.challenge, 300)
    return json.loads(webauthn.options_to_json(options))


def verify_registration(player: Player, credential_json: str, device_name: str = "") -> PasskeyCredential:
    challenge = cache.get(f"webauthn_reg:{player.pk}")
    if not challenge:
        raise ValueError("Registration session expired.")
    credential = parse_registration_credential_json(credential_json)
    verification = webauthn.verify_registration_response(
        credential=credential,
        expected_challenge=challenge,
        expected_rp_id=settings.WEBAUTHN_RP_ID,
        expected_origin=settings.WEBAUTHN_ORIGIN,
    )
    return PasskeyCredential.objects.create(
        player=player,
        credential_id=verification.credential_id,
        public_key=verification.credential_public_key,
        sign_count=verification.sign_count,
        aaguid=str(verification.aaguid) if verification.aaguid else "",
        device_name=device_name,
    )


def authentication_options(email: str) -> dict:
    try:
        player = Player.objects.get(email=email)
    except Player.DoesNotExist:
        raise ValueError("No account found.")
    credentials = [
        PublicKeyCredentialDescriptor(id=bytes(c.credential_id))
        for c in player.passkeys.all()
    ]
    options = webauthn.generate_authentication_options(
        rp_id=settings.WEBAUTHN_RP_ID,
        allow_credentials=credentials,
    )
    cache.set(f"webauthn_auth:{player.pk}", options.challenge, 300)
    cache.set(f"webauthn_challenge_player:{options.challenge.hex()}", player.pk, 300)
    return json.loads(webauthn.options_to_json(options))


def verify_authentication(credential_json: str) -> Player:
    credential = parse_authentication_credential_json(credential_json)
    client_data = json.loads(credential.response.client_data_json)
    challenge_bytes = base64url_to_bytes(client_data["challenge"])
    player_pk = cache.get(f"webauthn_challenge_player:{challenge_bytes.hex()}")
    if not player_pk:
        raise ValueError("Authentication session expired.")
    player = Player.objects.get(pk=player_pk)
    challenge = cache.get(f"webauthn_auth:{player.pk}")
    if not challenge:
        raise ValueError("Authentication session expired.")
    credential_id_bytes = bytes(credential.raw_id)
    passkey = PasskeyCredential.objects.get(credential_id=credential_id_bytes, player=player)
    verification = webauthn.verify_authentication_response(
        credential=credential,
        expected_challenge=challenge,
        expected_rp_id=settings.WEBAUTHN_RP_ID,
        expected_origin=settings.WEBAUTHN_ORIGIN,
        credential_public_key=bytes(passkey.public_key),
        credential_current_sign_count=passkey.sign_count,
    )
    passkey.sign_count = verification.new_sign_count
    passkey.last_used_at = timezone.now()
    passkey.save(update_fields=["sign_count", "last_used_at"])
    return player
