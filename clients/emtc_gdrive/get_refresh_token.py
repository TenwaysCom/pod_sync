#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests


DEFAULT_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DEFAULT_REDIRECT_URI = "https://developers.google.com/oauthplayground"
CONFIG_SECTION_KEY = "emtc_gdrive"
LEGACY_CONFIG_SECTION_KEY = "gdrive"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ABOUT_URL = "https://www.googleapis.com/drive/v3/about"


def load_gdrive_config(config_path):
    data = json.loads(Path(config_path).read_text(encoding="utf-8"))
    return data, data.get(CONFIG_SECTION_KEY) or data.get(LEGACY_CONFIG_SECTION_KEY, {})


def save_config(config_path, data):
    path = Path(config_path)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp_path, 0o600)
    tmp_path.replace(path)
    os.chmod(path, 0o600)


def build_auth_url(client_id, scope, redirect_uri, login_hint):
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_URL}?{urlencode(params)}"


def extract_auth_code(raw):
    value = (raw or "").strip().strip('"').strip("'")
    if not value:
        raise SystemExit("Authorization code is empty.")
    if "://" in value or value.startswith("code="):
        parsed = urlparse(value)
        query = parse_qs(parsed.query)
        fragment = parse_qs(parsed.fragment)
        code = (query.get("code") or fragment.get("code") or [None])[0]
        if not code:
            raise SystemExit("Could not find code= in the pasted URL.")
        return code
    return value


def exchange_code(client_id, client_secret, redirect_uri, code, timeout):
    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=timeout,
    )
    payload = response.json() if response.content else {}
    if response.status_code != 200:
        raise SystemExit(f"Google token exchange failed: {payload}")
    if not payload.get("access_token"):
        raise SystemExit("Google token exchange succeeded but no access_token was returned.")
    if not payload.get("refresh_token"):
        raise SystemExit(
            "Google did not return a refresh_token. Re-run with prompt=consent and accept Drive access again."
        )
    return payload


def fetch_user_email(access_token, timeout):
    response = requests.get(
        ABOUT_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"fields": "user(displayName,emailAddress)"},
        timeout=timeout,
    )
    response.raise_for_status()
    user = (response.json() or {}).get("user") or {}
    return user.get("emailAddress") or "", user.get("displayName") or ""


def main():
    parser = argparse.ArgumentParser(
        description="Get Google Drive refresh token using the existing OAuth Playground redirect."
    )
    parser.add_argument("--config", help="config.json path. Reads emtc_gdrive.client_id/client_secret.")
    parser.add_argument("--client-id")
    parser.add_argument("--client-secret")
    parser.add_argument("--scope", default=None, help=f"OAuth scopes. Default: {DEFAULT_SCOPE}")
    parser.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI)
    parser.add_argument("--login-hint", default="taiduo.yi@tenways.com")
    parser.add_argument("--auth-code", help="Authorization code, or the full redirected Playground URL.")
    parser.add_argument("--print-url", action="store_true", help="Print the Google consent URL and exit.")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--write-config", action="store_true")
    args = parser.parse_args()

    config_data = None
    gdrive_cfg = {}
    if args.config:
        config_data, gdrive_cfg = load_gdrive_config(args.config)

    client_id = args.client_id or gdrive_cfg.get("client_id")
    client_secret = args.client_secret or gdrive_cfg.get("client_secret")
    scope = args.scope or gdrive_cfg.get("scope") or DEFAULT_SCOPE
    redirect_uri = args.redirect_uri

    if not client_id or not client_secret:
        raise SystemExit("client_id and client_secret are required.")

    auth_url = build_auth_url(client_id, scope, redirect_uri, args.login_hint)
    if args.print_url:
        print(auth_url)
        return
    if not args.auth_code:
        print(auth_url)
        raise SystemExit(
            "Open the URL above as taiduo.yi@tenways.com, accept Drive access, "
            "then re-run with --auth-code <code or redirected URL>."
        )

    tokens = exchange_code(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        code=extract_auth_code(args.auth_code),
        timeout=args.timeout,
    )
    email, display_name = fetch_user_email(tokens["access_token"], args.timeout)
    print(json.dumps(
        {
            "email": email,
            "display_name": display_name,
            "scope": tokens.get("scope") or scope,
            "expires_in": tokens.get("expires_in"),
            "has_refresh_token": True,
        },
        ensure_ascii=False,
        indent=2,
    ))

    if args.write_config:
        if not args.config or config_data is None:
            raise SystemExit("--write-config requires --config.")
        if CONFIG_SECTION_KEY in config_data:
            target_key = CONFIG_SECTION_KEY
        elif LEGACY_CONFIG_SECTION_KEY in config_data:
            target_key = LEGACY_CONFIG_SECTION_KEY
        else:
            target_key = CONFIG_SECTION_KEY
            config_data[target_key] = {}
        config_data[target_key]["client_id"] = client_id
        config_data[target_key]["client_secret"] = client_secret
        config_data[target_key]["scope"] = scope
        config_data[target_key]["access_token"] = tokens["access_token"]
        config_data[target_key]["refresh_token"] = tokens["refresh_token"]
        save_config(args.config, config_data)
        print(f"Wrote updated Google Drive tokens to {args.config}")


if __name__ == "__main__":
    main()
