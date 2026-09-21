"""Native PIM authorization-code sign-in with a short-lived loopback callback.

This lets Microsoft evaluate the interactive browser sign-in normally. It
does not import portal tokens or alter Conditional Access. PKCE binds the
code to this process; state and nonce bind the response to this attempt.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import sys
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from . import clients, oauth, pim_setup
from .jwt import decode_jwt_segment
from .scopes import PIM_SCOPE


def _callback_handler(state: str, received: dict[str, str]) -> type[BaseHTTPRequestHandler]:
    class Callback(BaseHTTPRequestHandler):
        def setup(self) -> None:
            self.request.settimeout(5)
            super().setup()

        def log_message(self, format: str, *args: Any) -> None:
            # The request URL contains the authorization code. Never log it.
            pass

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            valid = (
                parsed.path == "/"
                and query.get("state") == [state]
                and len(query.get("code", query.get("error", []))) == 1
            )
            if valid:
                if "error" in query:
                    name = query["error"][0]
                    received["error"] = (
                        name if re.fullmatch(r"[a-z_]{1,64}", name) else "authorization denied"
                    )
                    codes = re.findall(
                        r"AADSTS[0-9]{4,10}", query.get("error_description", [""])[0]
                    )
                    received["error"] += " " + ", ".join(dict.fromkeys(codes))
                elif query.get("code", [""])[0]:
                    received["code"] = query["code"][0]
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(
                b"Return to owa-piggy to check the sign-in result."
                if valid
                else b"Invalid sign-in callback."
            )

    return Callback


def sign_in(alias: str, config: dict[str, str]) -> int:
    tenant = config.get("OWA_TENANT_ID", "").strip()
    email = config.get("OWA_EMAIL", "").strip()
    if not tenant or not email or config.get("OWA_PROVIDER", "msal") != "msal":
        print(
            "ERROR: PIM requires an existing Microsoft profile with tenant and email",
            file=sys.stderr,
        )
        return 1
    verifier, state, nonce = (secrets.token_urlsafe(32) for _ in range(3))
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    received: dict[str, str] = {}
    try:
        with HTTPServer(("127.0.0.1", 0), _callback_handler(state, received)) as server:
            server.timeout = 1
            redirect = f"http://localhost:{server.server_port}"
            params = {
                "client_id": oauth.PIM_CLIENT_ID,
                "response_type": "code",
                "redirect_uri": redirect,
                "response_mode": "query",
                "scope": PIM_SCOPE,
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "login_hint": email,
            }
            url = (
                f"https://login.microsoftonline.com/{urllib.parse.quote(tenant, safe='')}"
                "/oauth2/v2.0/authorize?" + urllib.parse.urlencode(params)
            )
            print(
                f"[{alias}] Opening browser sign-in. Complete it within 5 minutes.",
                file=sys.stderr,
                flush=True,
            )
            if not webbrowser.open(url):
                print(
                    "ERROR: Could not open the browser; existing credentials preserved",
                    file=sys.stderr,
                )
                return 1
            deadline = time.monotonic() + 300
            while not received and time.monotonic() < deadline:
                server.handle_request()
        if "code" not in received:
            if received.get("error"):
                print("ERROR: " + received["error"], file=sys.stderr)
            print(
                "ERROR: PIM browser sign-in denied or timed out; existing credentials preserved",
                file=sys.stderr,
            )
            return 1
        result = pim_setup._post(
            tenant,
            "token",
            {
                "client_id": oauth.PIM_CLIENT_ID,
                "grant_type": "authorization_code",
                "code": received["code"],
                "redirect_uri": redirect,
                "code_verifier": verifier,
                "scope": PIM_SCOPE,
            },
        )
        pim_setup._verify_identity(result, config)
        claims = decode_jwt_segment(result["id_token"].split(".")[1])
        if claims.get("nonce") != nonce:
            raise ValueError("nonce mismatch")
        clients.save_client(alias, oauth.PIM_CLIENT_ID, refresh_token=result["refresh_token"])
        print(f"[{alias}] PIM browser sign-in verified and saved", file=sys.stderr)
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print(
            "ERROR: PIM browser sign-in failed validation or transport; credentials preserved",
            file=sys.stderr,
        )
        return 1
