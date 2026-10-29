#!/usr/bin/env python3
import argparse
import json
import os
import time
from pathlib import Path

import requests


DEFAULT_SCOPE = "offline_access openid profile Files.Read User.Read"
CONFIG_SECTION_KEY = "cmc_onedrive"
LEGACY_CONFIG_SECTION_KEY = "onedrive"
GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me"


def load_onedrive_config(config_path):
    data = json.loads(Path(config_path).read_text(encoding="utf-8"))
    return data, data.get(CONFIG_SECTION_KEY) or data.get(LEGACY_CONFIG_SECTION_KEY, {})


def save_config(config_path, data):
    path = Path(config_path)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp_path, 0o600)
    tmp_path.replace(path)
    os.chmod(path, 0o600)


def build_base_url(tenant_id):
    tenant = tenant_id or "common"
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"


def request_device_code(base_url, client_id, scope, timeout):
    response = requests.post(
        f"{base_url}/devicecode",
        data={
            "client_id": client_id,
            "scope": scope,
        },
        timeout=timeout,
    )
    if response.status_code != 200:
        payload = response.json() if response.content else {}
        error = payload.get("error")
        description = " ".join(str(payload.get("error_description") or "").split())[:300]
        raise SystemExit(
            "Device code request failed"
            + (f" ({error})." if error else ".")
            + (f"\n{description}" if description else "")
        )
    return response.json()


def poll_for_tokens(base_url, client_id, device_code, interval, expires_in, timeout):
    deadline = time.time() + int(expires_in)
    while time.time() < deadline:
        response = requests.post(
            f"{base_url}/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": device_code,
            },
            timeout=timeout,
        )

        if response.status_code == 200:
            return response.json()

        payload = response.json() if response.content else {}
        error = payload.get("error")
        if error == "authorization_pending":
            time.sleep(int(interval))
            continue
        if error == "slow_down":
            time.sleep(int(interval) + 5)
            continue
        if error == "expired_token":
            raise SystemExit("Device code expired before login completed. Re-run to get a new code.")
        if error == "authorization_declined":
            raise SystemExit("Login was declined in the browser. Re-run to get a new code.")
        description = " ".join(str(payload.get("error_description") or "").split())[:300]
        raise SystemExit(
            f"Device code login failed: {error or 'unknown error'}."
            + (f"\n{description}" if description else "")
        )

    raise SystemExit("Device code expired before login completed.")


def describe_account(access_token, timeout):
    """Return (account_label, display_name) without ever echoing the token."""
    try:
        response = requests.get(
            GRAPH_ME_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return f"<verification failed: {type(exc).__name__}>", ""
    if response.status_code != 200:
        return f"<verification failed: HTTP {response.status_code}>", ""
    payload = response.json() or {}
    account = (
        payload.get("userPrincipalName")
        or payload.get("mail")
        or payload.get("id")
        or "<unknown>"
    )
    return account, payload.get("displayName") or ""


def main():
    parser = argparse.ArgumentParser(description="Get OneDrive refresh token with Microsoft device code flow.")
    parser.add_argument("--config", help="Optional config.json path. If provided, client_id/tenant_id/scope can be read from cmc_onedrive config.")
    parser.add_argument("--client-id", help="Microsoft Entra Application (client) ID.")
    parser.add_argument("--tenant-id", help="Tenant ID or alias, default common.")
    parser.add_argument("--scope", default=None, help="OAuth scopes. Default: offline_access openid profile Files.Read User.Read")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--write-config", action="store_true", help="Write access_token/refresh_token/client_id/tenant_id/scope back to config.json")
    parser.add_argument("--no-verify", action="store_true", help="Skip the Graph /me check that confirms which account was authorized")
    args = parser.parse_args()

    config_data = None
    onedrive_cfg = {}
    if args.config:
        config_data, onedrive_cfg = load_onedrive_config(args.config)

    client_id = args.client_id or onedrive_cfg.get("client_id")
    tenant_id = args.tenant_id or onedrive_cfg.get("tenant_id") or "common"
    scope = args.scope or onedrive_cfg.get("scope") or DEFAULT_SCOPE

    if not client_id:
        raise SystemExit("client_id is required. Pass --client-id or set cmc_onedrive.client_id in config.json.")

    base_url = build_base_url(tenant_id)
    device = request_device_code(base_url, client_id, scope, args.timeout)
    verification_uri = device.get("verification_uri") or device.get("verification_uri_complete") or ""
    user_code = device.get("user_code") or ""
    print(device.get("message") or f"Open {verification_uri} and enter code {user_code}")
    print()

    tokens = poll_for_tokens(
        base_url=base_url,
        client_id=client_id,
        device_code=device["device_code"],
        interval=device.get("interval", 5),
        expires_in=device.get("expires_in", 900),
        timeout=args.timeout,
    )

    access_token = tokens.get("access_token") or ""
    refresh_token = tokens.get("refresh_token") or ""
    if not access_token:
        raise SystemExit("Login succeeded but no access_token was returned.")
    if not refresh_token:
        raise SystemExit(
            "Login succeeded but no refresh_token was returned. "
            "The scope must include offline_access."
        )

    account = ""
    display_name = ""
    if not args.no_verify:
        account, display_name = describe_account(access_token, args.timeout)

    result = {
        "tenant_id": tenant_id,
        "client_id": client_id,
        "scope": tokens.get("scope") or scope,
        "account": account,
        "display_name": display_name,
        "has_access_token": True,
        "has_refresh_token": True,
        "expires_in": tokens.get("expires_in"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

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
        config_data[target_key]["tenant_id"] = tenant_id
        config_data[target_key]["client_id"] = client_id
        config_data[target_key]["scope"] = tokens.get("scope") or scope
        config_data[target_key]["access_token"] = access_token
        config_data[target_key]["refresh_token"] = refresh_token
        save_config(args.config, config_data)
        print(f"Wrote updated OneDrive tokens to {args.config}")


if __name__ == "__main__":
    main()
