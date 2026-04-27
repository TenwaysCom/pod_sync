#!/usr/bin/env python3
import argparse
import json
import time
from pathlib import Path

import requests


DEFAULT_SCOPE = "offline_access openid profile Files.Read User.Read"
CONFIG_SECTION_KEY = "cmc_onedrive"
LEGACY_CONFIG_SECTION_KEY = "onedrive"


def load_onedrive_config(config_path):
    data = json.loads(Path(config_path).read_text(encoding="utf-8"))
    return data, data.get(CONFIG_SECTION_KEY) or data.get(LEGACY_CONFIG_SECTION_KEY, {})


def save_config(config_path, data):
    Path(config_path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
    response.raise_for_status()
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

        payload = response.json()
        error = payload.get("error")
        if error == "authorization_pending":
            time.sleep(int(interval))
            continue
        if error == "slow_down":
            time.sleep(int(interval) + 5)
            continue
        raise SystemExit(f"Device code login failed: {payload}")

    raise SystemExit("Device code expired before login completed.")


def main():
    parser = argparse.ArgumentParser(description="Get OneDrive refresh token with Microsoft device code flow.")
    parser.add_argument("--config", help="Optional config.json path. If provided, client_id/tenant_id/scope can be read from cmc_onedrive config.")
    parser.add_argument("--client-id", help="Microsoft Entra Application (client) ID.")
    parser.add_argument("--tenant-id", help="Tenant ID or alias, default common.")
    parser.add_argument("--scope", default=None, help="OAuth scopes. Default: offline_access openid profile Files.Read User.Read")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--write-config", action="store_true", help="Write access_token/refresh_token/client_id/tenant_id/scope back to config.json")
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
    print(device.get("message") or f"Open {device['verification_uri']} and enter code {device['user_code']}")

    tokens = poll_for_tokens(
        base_url=base_url,
        client_id=client_id,
        device_code=device["device_code"],
        interval=device.get("interval", 5),
        expires_in=device.get("expires_in", 900),
        timeout=args.timeout,
    )

    result = {
        "tenant_id": tenant_id,
        "client_id": client_id,
        "scope": scope,
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
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
        config_data[target_key]["scope"] = scope
        if result.get("access_token"):
            config_data[target_key]["access_token"] = result["access_token"]
        if result.get("refresh_token"):
            config_data[target_key]["refresh_token"] = result["refresh_token"]
        save_config(args.config, config_data)
        print(f"Wrote updated OneDrive tokens to {args.config}")


if __name__ == "__main__":
    main()
