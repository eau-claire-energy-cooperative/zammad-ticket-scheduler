#!/usr/bin/env python3
"""
zammad_create_ticket.py

Create one or more Zammad tickets via API from a YAML config file.

- Reads Zammad URL and token from config.
- Best practice: store token in an environment variable and reference it via zammad.token_env.
- Creates one Zammad ticket per entry in tickets[].
- Supports optional per-ticket owner or owner_id.
- Supports defaults (group, article fields) with per-ticket overrides.

Exit codes:
  0 success (all tickets created)
  2 bad usage / bad config
  4 one or more tickets failed (API or validation)
  5 networking error (if it occurs and prevents creating ticket(s))
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import yaml


def http_json(
    method: str,
    url: str,
    token: str,
    payload: dict | None = None,
    timeout: int = 30,
) -> tuple[int, dict | list | str]:
    """
    Make an HTTP request and return (status_code, parsed_response).

    - Uses Zammad Token auth header
    - JSON-encodes payload when provided
    - Parses JSON responses automatically when content-type indicates JSON
    - Raises RuntimeError for HTTP errors and ConnectionError for network issues
    """
    headers = {
        "Authorization": f"Token token={token}",
        "Accept": "application/json",
    }

    # Encode payload as JSON if present
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url=url, data=data, headers=headers, method=method)

    try:
        # Execute request and read raw response body
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            ct = resp.headers.get("Content-Type", "")

            # Return JSON-decoded object when possible
            if "application/json" in ct:
                return resp.status, json.loads(raw) if raw else {}

            return resp.status, raw

    except urllib.error.HTTPError as e:
        # Surface API error responses with useful details
        body = e.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(body) if body else {"error": str(e)}
            raise RuntimeError(f"HTTP {e.code}: {err_json}") from None
        except json.JSONDecodeError:
            raise RuntimeError(f"HTTP {e.code}: {body or str(e)}") from None

    except urllib.error.URLError as e:
        # Covers DNS failure, refused connection, etc.
        raise ConnectionError(str(e)) from None


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI arguments."""
    p = argparse.ArgumentParser(description="Create Zammad tickets via API from YAML.")
    p.add_argument("--config", required=True, help="Path to YAML config file")
    return p.parse_args(argv)


def require_nonempty_str(v: Any, field: str, ctx: str) -> str:
    """
    Validate that a value is a non-empty string and return the trimmed value.

    Used throughout config validation to produce consistent error messages.
    """
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{ctx}: '{field}' is required and must be a non-empty string")
    return v.strip()


def deep_merge_dicts(base: dict, override: dict) -> dict:
    """
    Recursively merge override into base, returning a new dict.

    - Nested dicts are merged recursively
    - Non-dict values overwrite base values
    """
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge_dicts(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str) -> dict[str, Any]:
    """
    Load and validate the YAML config file structure.

    Ensures:
      - root is a dict
      - zammad section exists
      - tickets is a non-empty list
      - defaults (if provided) is a dict
    """
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ValueError("Config root must be a mapping/object")
    if "zammad" not in data or not isinstance(data["zammad"], dict):
        raise ValueError("Config must contain 'zammad:' as a mapping/object")

    tickets = data.get("tickets")
    if not isinstance(tickets, list) or len(tickets) == 0:
        raise ValueError("Config must contain 'tickets:' as a non-empty list")

    defaults = data.get("defaults", {})
    if defaults and not isinstance(defaults, dict):
        raise ValueError("'defaults' must be a mapping/object if provided")

    return data


def get_zammad_auth(cfg: dict[str, Any]) -> tuple[str, str, int]:
    """
    Extract base URL, API token, and timeout from config.

    Token can be provided as:
      - zammad.token (inline)
      - zammad.token_env (environment variable name)

    Returns:
      (base_url, token, timeout)
    """
    z = cfg["zammad"]
    base_url = require_nonempty_str(z.get("url"), "url", "zammad").rstrip("/")

    # Timeout is optional but must be a positive integer
    timeout = z.get("timeout", 30)
    if not isinstance(timeout, int) or timeout <= 0:
        raise ValueError("zammad.timeout must be a positive integer if provided")

    token = z.get("token")
    token_env = z.get("token_env")

    # Prevent ambiguous auth configuration
    if token is not None and token_env is not None:
        raise ValueError("Use either zammad.token OR zammad.token_env, not both")

    # Preferred method: token pulled from environment
    if token_env is not None:
        token_env = require_nonempty_str(token_env, "token_env", "zammad")
        env_val = os.environ.get(token_env, "")
        if not env_val.strip():
            raise ValueError(f"Environment variable '{token_env}' is not set or empty")
        token = env_val.strip()
    else:
        token = require_nonempty_str(token, "token", "zammad")

    return base_url, token, timeout


def resolve_owner_id(base: str, token: str, owner_query: str, timeout: int) -> int:
    """
    Resolve a user query (email/login/username) into a numeric owner_id.

    Uses /api/v1/users/search and attempts to narrow to exact matches.
    Raises if no match or multiple plausible matches are found.
    """
    q = urllib.parse.quote(owner_query, safe="")
    url = f"{base}/api/v1/users/search?query={q}"

    _, resp = http_json("GET", url, token, timeout=timeout)

    # Validate response type from Zammad API
    if not isinstance(resp, list):
        raise RuntimeError(f"Unexpected users/search response type: {type(resp).__name__}")
    if not resp:
        raise RuntimeError(f"Owner not found for query: {owner_query}")

    target = owner_query.strip().lower()

    # Helper to normalize possible user fields for comparison
    def field(u: dict, k: str) -> str:
        v = u.get(k)
        return v.strip().lower() if isinstance(v, str) else ""

    # Prefer exact matches if possible
    exact: list[dict] = []
    for u in resp:
        if isinstance(u, dict) and target in (field(u, "email"), field(u, "login"), field(u, "username")):
            exact.append(u)

    candidates = exact if exact else [u for u in resp if isinstance(u, dict)]

    # If we only have one candidate, return its id
    if len(candidates) == 1:
        uid = candidates[0].get("id")
        if isinstance(uid, int):
            return uid
        raise RuntimeError(f"Resolved owner but missing numeric id in user record: {candidates[0]}")

    # Otherwise provide a summary to help the user disambiguate
    summary: list[str] = []
    for u in candidates[:10]:
        uid = u.get("id")
        email = u.get("email", "")
        login = u.get("login", "") or u.get("username", "")
        name = " ".join([x for x in [u.get("firstname", ""), u.get("lastname", "")] if x]).strip()
        summary.append(f"id={uid} login={login!s} email={email!s} name={name!s}")

    raise RuntimeError(
        "Owner query is ambiguous; matches:\n  - " + "\n  - ".join(summary)
        + "\nTip: use owner_id to set it directly, or pass a more specific email/login."
    )


def build_payload(ticket: dict, defaults: dict, ctx: str) -> tuple[dict, str, int]:
    """
    Build and validate the Zammad ticket payload.

    - Applies defaults first, then per-ticket overrides
    - Validates required fields
    - Returns (payload, owner_string, owner_id) for later resolution
    """
    merged = deep_merge_dicts(defaults, ticket)

    title = require_nonempty_str(merged.get("title"), "title", ctx)

    # group can be a string name or an integer id
    group = merged.get("group")
    if not ((isinstance(group, str) and group.strip()) or isinstance(group, int)):
        raise ValueError(f"{ctx}: 'group' is required (string name or integer id)")

    customer = require_nonempty_str(merged.get("customer"), "customer", ctx)

    # article must exist and be a mapping
    article = merged.get("article")
    if not isinstance(article, dict):
        raise ValueError(f"{ctx}: 'article' is required and must be a mapping/object")

    subject = require_nonempty_str(article.get("subject"), "subject", f"{ctx}.article")
    body = require_nonempty_str(article.get("body"), "body", f"{ctx}.article")

    # type defaults to "note"
    art_type = article.get("type", "note")
    art_type = require_nonempty_str(art_type, "type", f"{ctx}.article")

    # internal controls customer visibility
    internal = article.get("internal", False)
    if not isinstance(internal, bool):
        raise ValueError(f"{ctx}.article: 'internal' must be boolean true/false")

    # owner and owner_id are mutually exclusive
    owner = merged.get("owner", "")
    owner_id = merged.get("owner_id", 0)

    if owner and owner_id:
        raise ValueError(f"{ctx}: use either 'owner' or 'owner_id', not both")
    if owner and not isinstance(owner, str):
        raise ValueError(f"{ctx}: 'owner' must be a string if provided")
    if owner_id and not isinstance(owner_id, int):
        raise ValueError(f"{ctx}: 'owner_id' must be an integer if provided")

    payload: dict = {
        "title": title,
        "group": group,
        "customer": customer,
        "article": {
            "subject": subject,
            "body": body,
            "type": art_type,
            "internal": internal,
        },
    }

    return payload, (owner.strip() if isinstance(owner, str) else ""), (owner_id if isinstance(owner_id, int) else 0)


def main(argv: list[str]) -> int:
    """
    Programmatic entrypoint.

    Returns an exit code instead of calling sys.exit() directly so other modules
    (like scheduler.py) can call it safely.
    """
    args = parse_args(argv)

    # Load config and auth details
    try:
        cfg = load_config(args.config)
        base, token, timeout = get_zammad_auth(cfg)
    except Exception as e:
        print(f"Bad config/auth: {e}", file=sys.stderr)
        return 2

    defaults = cfg.get("defaults", {}) or {}
    tickets = cfg["tickets"]

    create_url = f"{base}/api/v1/tickets"

    any_failed = False
    any_network_error = False

    # Process each ticket independently so one failure doesn't stop the batch
    for i, t in enumerate(tickets, start=1):
        ctx = f"tickets[{i}]"

        if not isinstance(t, dict):
            print(f"{ctx}: must be a mapping/object", file=sys.stderr)
            any_failed = True
            continue

        # Build and validate payload
        try:
            payload, owner, owner_id = build_payload(t, defaults, ctx)
        except Exception as e:
            print(f"{ctx}: invalid ticket: {e}", file=sys.stderr)
            any_failed = True
            continue

        # Resolve owner if needed
        if owner_id and owner_id > 0:
            payload["owner_id"] = owner_id
        elif owner:
            try:
                payload["owner_id"] = resolve_owner_id(base, token, owner, timeout)
            except ConnectionError as e:
                print(f"{ctx}: network error resolving owner: {e}", file=sys.stderr)
                any_failed = True
                any_network_error = True
                continue
            except RuntimeError as e:
                print(f"{ctx}: API error resolving owner: {e}", file=sys.stderr)
                any_failed = True
                continue

        # Create the ticket via Zammad API
        try:
            status, resp = http_json("POST", create_url, token, payload=payload, timeout=timeout)
            print(
                json.dumps(
                    {"ticket_index": i, "http_status": status, "response": resp},
                    indent=2,
                    ensure_ascii=False,
                )
            )
        except ConnectionError as e:
            print(f"{ctx}: network error creating ticket: {e}", file=sys.stderr)
            any_failed = True
            any_network_error = True
        except RuntimeError as e:
            print(f"{ctx}: API error creating ticket: {e}", file=sys.stderr)
            any_failed = True

    # Use distinct exit codes to make automation easier
    if any_network_error:
        return 5
    return 4 if any_failed else 0


if __name__ == "__main__":
    # Ensure the return code becomes the program exit code
    raise SystemExit(main(sys.argv[1:]))
