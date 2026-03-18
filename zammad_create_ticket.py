#!/usr/bin/env python3
"""
zammad_create_ticket.py

Create one or more Zammad tickets via API from a YAML config file.

- Reads Zammad URL and token from config.
- Preferred: store token in an environment variable and reference it via zammad.token_env.
- Creates one Zammad ticket per entry in tickets[].
- Supports optional per-ticket owner (email/login) or owner_id.
- Supports defaults with per-ticket overrides (simple merge).

Exit codes:
  0 success (all tickets created)
  2 bad usage / bad config
  4 one or more tickets failed (API or validation)
  5 networking error (prevented creating ticket(s))
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import logging
import urllib.error
import urllib.parse
import urllib.request

import yaml


def setup_logging() -> logging.Logger:
    """Log to stdout/stderr so `docker logs` captures it."""
    logger = logging.getLogger("zammad_create_ticket")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    out = logging.StreamHandler(sys.stdout)
    out.setLevel(logging.INFO)
    out.setFormatter(fmt)

    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING)
    err.setFormatter(fmt)

    logger.addHandler(out)
    logger.addHandler(err)
    return logger


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI arguments."""
    p = argparse.ArgumentParser(description="Create Zammad tickets via API from YAML.")
    p.add_argument("--config", required=True, help="Path to YAML config file")
    return p.parse_args(argv)


def http_json(method: str, url: str, token: str, payload: dict | None = None, timeout: int = 30):
    """
    Make an HTTP request and return (status_code, parsed_response).

    Raises:
      - RuntimeError for HTTP errors (4xx/5xx)
      - ConnectionError for network issues (DNS, refused, timeout, etc.)
    """
    headers = {
        "Authorization": f"Token token={token}",
        "Accept": "application/json",
    }

    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url=url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            ct = resp.headers.get("Content-Type", "")
            if "application/json" in ct:
                return resp.status, (json.loads(raw) if raw else {})
            return resp.status, raw

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        # Keep this simple: try JSON, else raw text
        try:
            msg = json.loads(body) if body else {"error": str(e)}
        except json.JSONDecodeError:
            msg = body or str(e)
        raise RuntimeError(f"HTTP {e.code}: {msg}") from None

    except urllib.error.URLError as e:
        raise ConnectionError(str(e)) from None


def load_config(path: str) -> dict:
    """Load YAML into a dict (minimal validation)."""
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        raise ValueError("Config root must be a mapping/object")
    return cfg


def get_auth(cfg: dict) -> tuple[str, str, int]:
    """
    Extract base URL, token, timeout.

    Token:
      - zammad.token (inline) OR
      - zammad.token_env (env var name)
    """
    z = cfg.get("zammad") or {}
    if not isinstance(z, dict):
        raise ValueError("Config must contain 'zammad:' as a mapping/object")

    # URL comes from environment (provided via docker compose .env)
    base = os.environ.get("ZAMMAD_URL", "").strip().rstrip("/")
    if not base:
        raise ValueError("ZAMMAD_URL environment variable is required")

    timeout = z.get("timeout", 30)
    try:
        timeout = int(timeout)
    except Exception:
        timeout = 30
    if timeout <= 0:
        timeout = 30

    # Token comes from environment (provided via docker compose .env)
    token = os.environ.get("ZAMMAD_TOKEN", "").strip()
    if not token:
        raise ValueError("ZAMMAD_TOKEN environment variable is required")

    return base, token, timeout


def resolve_owner_id(base: str, token: str, owner_query: str, timeout: int) -> int:
    """
    Resolve owner (email/login/username) to a numeric user id via /api/v1/users/search.

    If multiple matches exist, we fail with a short summary (simple on purpose).
    """
    q = urllib.parse.quote(owner_query.strip(), safe="")
    url = f"{base}/api/v1/users/search?query={q}"
    _, resp = http_json("GET", url, token, timeout=timeout)

    if not isinstance(resp, list) or not resp:
        raise RuntimeError(f"Owner not found for query: {owner_query}")

    # Prefer exact-ish matches on common fields if present
    target = owner_query.strip().lower()
    exact = []
    for u in resp:
        if not isinstance(u, dict):
            continue
        for k in ("email", "login", "username"):
            v = u.get(k)
            if isinstance(v, str) and v.strip().lower() == target:
                exact.append(u)
                break

    candidates = exact or [u for u in resp if isinstance(u, dict)]
    if len(candidates) == 1 and isinstance(candidates[0].get("id"), int):
        return candidates[0]["id"]

    # Ambiguous: provide a compact summary
    lines = []
    for u in candidates[:10]:
        lines.append(f"id={u.get('id')} email={u.get('email')} login={u.get('login') or u.get('username')}")
    raise RuntimeError("Owner query ambiguous; matches:\n  - " + "\n  - ".join(lines))

def attach_checklist(base: str, token: str, ticket_id: int, template_id: int, timeout: int):
    """Attach a checklist template to a ticket."""
    url = f"{base}/api/v1/checklists"
    payload = {
        "ticket_id": ticket_id,
        "template_id": template_id,
    }
    return http_json("POST", url, token, payload=payload, timeout=timeout)

def merge_ticket(defaults: dict, ticket: dict) -> dict:
    """
    Simple defaults merge:
      - top-level keys: defaults overwritten by ticket
      - article: shallow merge defaults['article'] and ticket['article']
    """
    merged = dict(defaults or {})
    merged.update(ticket or {})

    d_art = (defaults or {}).get("article") if isinstance((defaults or {}).get("article"), dict) else {}
    t_art = (ticket or {}).get("article") if isinstance((ticket or {}).get("article"), dict) else {}
    if d_art or t_art:
        art = dict(d_art)
        art.update(t_art)
        merged["article"] = art

    return merged


def build_payload(merged: dict, ctx: str) -> tuple[dict, str, int, int]:
    """
    Build the ticket payload with only essential validation.
    Returns: (payload, owner_string, owner_id, checklist_template_id)
    """
    title = str(merged.get("title", "")).strip()
    customer = str(merged.get("customer", "")).strip()
    group = merged.get("group")

    if not title:
        raise ValueError(f"{ctx}: title is required")
    if not customer:
        raise ValueError(f"{ctx}: customer is required")
    if group is None or (isinstance(group, str) and not group.strip()):
        raise ValueError(f"{ctx}: group is required (string name or integer id)")

    article = merged.get("article") or {}
    if not isinstance(article, dict):
        raise ValueError(f"{ctx}: article must be a mapping/object")

    subject = str(article.get("subject", "")).strip()
    body = str(article.get("body", "")).strip()
    if not subject:
        raise ValueError(f"{ctx}: article.subject is required")
    if not body:
        raise ValueError(f"{ctx}: article.body is required")

    art_type = str(article.get("type", "note")).strip() or "note"
    internal = article.get("internal", False)
    internal = bool(internal)  # accept truthy/falsey without lots of type checks

    owner = merged.get("owner") or ""
    owner_id = merged.get("owner_id") or 0

    owner = owner.strip() if isinstance(owner, str) else ""
    owner_id = int(owner_id) if isinstance(owner_id, int) or (isinstance(owner_id, str) and owner_id.isdigit()) else 0

    if owner and owner_id:
        raise ValueError(f"{ctx}: use either owner OR owner_id, not both")
        
    checklist_template_id = merged.get("checklist_template_id") or 0
    if isinstance(checklist_template_id, str):
        checklist_template_id = checklist_template_id.strip()

    if checklist_template_id in ("", None, 0, "0"):
        checklist_template_id = 0
    elif isinstance(checklist_template_id, int):
        pass
    elif isinstance(checklist_template_id, str) and checklist_template_id.isdigit():
        checklist_template_id = int(checklist_template_id)
    else:
        raise ValueError(f"{ctx}: checklist_template_id must be an integer if provided")

    payload = {
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

    return payload, owner, owner_id, checklist_template_id


def main(argv: list[str]) -> int:
    """
    Programmatic entrypoint.

    Returns an exit code instead of sys.exit() so scheduler.py can call it.
    """
    logger = setup_logging()

    try:
        args = parse_args(argv)
        cfg = load_config(args.config)
        base, token, timeout = get_auth(cfg)
    except Exception as e:
        logger.error(f"Bad config/auth: {e}")
        return 2

    tickets = cfg.get("tickets") or []
    if not isinstance(tickets, list) or not tickets:
        logger.error("Config must contain 'tickets:' as a non-empty list")
        return 2

    defaults = cfg.get("defaults") or {}
    if defaults and not isinstance(defaults, dict):
        logger.error("'defaults' must be a mapping/object if provided")
        return 2

    create_url = f"{base}/api/v1/tickets"

    any_failed = False
    any_network_error = False

    for i, t in enumerate(tickets, start=1):
        ctx = f"tickets[{i}]"

        if not isinstance(t, dict):
            logger.error(f"{ctx}: must be a mapping/object")
            any_failed = True
            continue

        merged = merge_ticket(defaults, t)

        try:
            payload, owner, owner_id, checklist_template_id = build_payload(merged, ctx)
        except Exception as e:
            logger.error(f"{ctx}: invalid ticket: {e}")
            any_failed = True
            continue

        # Resolve owner if needed
        try:
            if owner_id > 0:
                payload["owner_id"] = owner_id
            elif owner:
                payload["owner_id"] = resolve_owner_id(base, token, owner, timeout)
        except ConnectionError as e:
            logger.error(f"{ctx}: network error resolving owner: {e}")
            any_failed = True
            any_network_error = True
            continue
        except RuntimeError as e:
            logger.error(f"{ctx}: API error resolving owner: {e}")
            any_failed = True
            continue

        # Create the ticket
        try:
            status, resp = http_json("POST", create_url, token, payload=payload, timeout=timeout)
            ticket_id = resp.get("id") if isinstance(resp, dict) else None
            logger.info(f"{ctx}: created (HTTP {status}) ticket_id={ticket_id}")
        except ConnectionError as e:
            logger.error(f"{ctx}: network error creating ticket: {e}")
            any_failed = True
            any_network_error = True
            continue
        except RuntimeError as e:
            logger.error(f"{ctx}: API error creating ticket: {e}")
            any_failed = True
            continue

        if checklist_template_id and ticket_id:
            try:
                c_status, _ = attach_checklist(
                    base, token, int(ticket_id), checklist_template_id, timeout
                )
                logger.info(
                    f"{ctx}: attached checklist template_id={checklist_template_id} "
                    f"to ticket_id={ticket_id} (HTTP {c_status})"
                )
            except ConnectionError as e:
                logger.error(f"{ctx}: network error attaching checklist: {e}")
                any_failed = True
                any_network_error = True
            except RuntimeError as e:
                logger.error(f"{ctx}: API error attaching checklist: {e}")
                any_failed = True

    if any_network_error:
        return 5
    return 4 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
