"""Explicit native-client PIM sign-in, owned entirely by the broker.

Microsoft Graph Command Line Tools has no SPA to capture. Its device authorization flow obtains a
separate refresh token after the user signs in. Never borrow a family token,
import another application's cache, or replace the profile's OWA credential.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import clients, oauth
from .jwt import decode_jwt_segment
from .scopes import PIM_PERMISSION, PIM_SCOPE


def _post(tenant: str, endpoint: str, fields: dict[str, str]) -> dict[str, Any]:
    url = (
        f"https://login.microsoftonline.com/{urllib.parse.quote(tenant, safe='')}"
        f"/oauth2/v2.0/{endpoint}"
    )
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode(),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with oauth._OPENER.open(request, timeout=oauth.EXCHANGE_TIMEOUT) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        result = json.loads(exc.read())
    if not isinstance(result, dict):
        raise ValueError("invalid OAuth response")
    return result


def _verify_identity(result: dict[str, Any], config: dict[str, str]) -> None:
    """Check the identity from the token endpoint before persisting anything.

    This decodes a token obtained directly over TLS from Microsoft; it is not
    a general-purpose verifier for user-supplied ID tokens.
    """
    try:
        if not isinstance(result.get("id_token"), str):
            raise ValueError("invalid ID token")
        claims = decode_jwt_segment(result["id_token"].split(".")[1])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError("PIM sign-in did not return a usable ID token") from exc
    if not isinstance(claims, dict) or not isinstance(claims.get("tid"), str):
        raise ValueError("PIM sign-in did not return usable identity claims")
    email = config.get("OWA_EMAIL", "").strip().casefold()
    identities = [
        str(claims.get(key, "")).casefold() for key in ("preferred_username", "upn", "email")
    ]
    if (
        claims.get("tid", "").casefold() != config["OWA_TENANT_ID"].casefold()
        or claims.get("aud") != oauth.PIM_CLIENT_ID
        or email not in identities
    ):
        raise ValueError("PIM sign-in identity differs from the selected profile; nothing saved")
    granted = str(result.get("scope", "")).split()
    if not any(value in granted for value in (PIM_PERMISSION, PIM_PERMISSION.rsplit("/", 1)[1])):
        raise ValueError("PIM permission was not granted; nothing saved")
    if any(
        not isinstance(result.get(key), str) or not result[key]
        for key in ("refresh_token", "access_token")
    ):
        raise ValueError("PIM sign-in returned incomplete credentials; nothing saved")


def sign_in(alias: str, config: dict[str, str]) -> int:
    """Perform one explicitly requested device sign-in for an existing profile."""
    tenant = config.get("OWA_TENANT_ID", "").strip()
    if not tenant or not config.get("OWA_EMAIL", "").strip():
        print(
            "ERROR: PIM setup requires an existing profile with tenant and email", file=sys.stderr
        )
        return 1
    if config.get("OWA_PROVIDER", "msal") != "msal":
        print("ERROR: PIM requires a Microsoft work profile", file=sys.stderr)
        return 1
    try:
        device = _post(
            tenant,
            "devicecode",
            {
                "client_id": oauth.PIM_CLIENT_ID,
                "scope": PIM_SCOPE,
            },
        )
        if device.get("error"):
            # Error descriptions can contain tenant/account data; keep raw
            # responses and device credentials out of diagnostics.
            print("ERROR: PIM device authorization rejected", file=sys.stderr)
            return 1
        code = device.get("user_code")
        device_code = device.get("device_code")
        if not isinstance(code, str) or not isinstance(device_code, str):
            raise ValueError("invalid device authorization response")
        lifetime = int(device["expires_in"])
        interval = int(device.get("interval", 5))
        if not 0 < lifetime <= 1800 or not 0 < interval <= 60:
            raise ValueError("invalid device authorization timing")
        print(
            f"[{alias}] Open https://microsoft.com/devicelogin and enter {code}. "
            "Sign in as the selected profile's account.",
            file=sys.stderr,
            flush=True,
        )
        deadline = time.monotonic() + lifetime
        while time.monotonic() + interval < deadline:
            time.sleep(interval)
            result = _post(
                tenant,
                "token",
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": oauth.PIM_CLIENT_ID,
                    "device_code": device_code,
                },
            )
            error = result.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error:
                print(
                    "ERROR: PIM sign-in was denied, expired, or blocked by tenant policy",
                    file=sys.stderr,
                )
                return 1
            _verify_identity(result, config)
            clients.save_client(alias, oauth.PIM_CLIENT_ID, refresh_token=result["refresh_token"])
            print(f"[{alias}] PIM sign-in verified and saved", file=sys.stderr)
            return 0
        print("ERROR: PIM device sign-in expired; run clients add pim again", file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError):
        print(
            "ERROR: PIM sign-in failed validation or transport; existing credentials preserved",
            file=sys.stderr,
        )
    return 1
