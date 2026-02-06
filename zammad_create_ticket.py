#!/usr/bin/env python3
"""
Create a Zammad ticket via API (cron-friendly, non-interactive).

Auth: Personal Access Token (PAT) via header: Authorization: Token token=...
** This can be created from the Zammad web interface and does not need to be
** made using API calls

Required arguments:
  --url (Base url for API calls)
  --token (PAT token from Auth note above)
  --title (Title of the ticket)
  --group (Group that the ticket should apply to (e.g. ECEC IT, Oakdale IT, etc.)
  --customer (Customer for the ticket - in the form of email address)
  --subject (Subject for the ticket)
  --body (Body of the first message on the ticket - this should be notes about what the ticket is for)

Optional:
  --type (default: note - See https://docs.zammad.org/en/latest/api/ticket/articles.html for other types)
  --internal (default: false - True would make it an internal note)
  --dedupe-key (Unique identifier to prevent the same task from running multiple times - recommended for cron)
  --owner (email or username/login; script resolves to owner_id)
  --owner-id (skip lookup; directly set owner_id)
  --timeout (length of time (s) before the post message fails out - default: 30)

Exit codes:
  0 success (or dedupe prevented duplicate)
  2 bad usage
  4 API error
  5 networking error
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
import urllib.error


def http_json(
    method: str,
    url: str,
    token: str,
    payload: dict | None = None,
    timeout: int = 30,
) -> tuple[int, dict | list | str]:
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
                return resp.status, json.loads(raw) if raw else {}
            return resp.status, raw
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(body) if body else {"error": str(e)}
            raise RuntimeError(f"HTTP {e.code}: {err_json}") from None
        except json.JSONDecodeError:
            raise RuntimeError(f"HTTP {e.code}: {body or str(e)}") from None
    except urllib.error.URLError as e:
        raise ConnectionError(str(e)) from None


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Create a Zammad ticket via API.")
    p.add_argument("--url", required=True, help="Zammad base URL, e.g. https://zammad.example.com")
    p.add_argument("--token", required=True, help="Zammad Personal Access Token")

    p.add_argument("--title", required=True, help="Ticket title")
    p.add_argument("--group", required=True, help="Group name or ID")
    p.add_argument("--customer", required=True, help="Customer email/login")

    p.add_argument("--subject", required=True, help="Article subject")
    p.add_argument("--body", required=True, help="Article body")

    p.add_argument("--type", default="note", help="Article type (default: note)")
    p.add_argument(
        "--internal",
        default="false",
        choices=["true", "false"],
        help="Whether the article is internal (default: false)",
    )

    p.add_argument(
        "--dedupe-key",
        default="",
        help="If provided, script will avoid creating a duplicate ticket for this key (cron-safe).",
    )

    # Owner options:
    p.add_argument(
        "--owner",
        default="",
        help="Owner email or username/login. Script resolves this to owner_id via /users/search.",
    )
    p.add_argument(
        "--owner-id",
        type=int,
        default=0,
        help="Owner agent user ID (skips lookup).",
    )

    p.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds (default: 30)")
    return p.parse_args(argv)


def resolve_owner_id(base: str, token: str, owner_query: str, timeout: int) -> int:
    """
    Resolve owner_query (email or login/username) to a unique user id via:
      GET /api/v1/users/search?query=<owner_query>

    Selection logic:
      - Prefer exact (case-insensitive) match on: email OR login OR username
      - If exactly one exact match -> return id
      - If none exact but only one result -> return id (fallback)
      - Otherwise fail (ambiguous or not found)
    """
    q = urllib.parse.quote(owner_query, safe="")
    url = f"{base}/api/v1/users/search?query={q}"

    _, resp = http_json("GET", url, token, timeout=timeout)

    if not isinstance(resp, list):
        raise RuntimeError(f"Unexpected users/search response type: {type(resp).__name__}")

    if len(resp) == 0:
        raise RuntimeError(f"Owner not found for query: {owner_query}")

    target = owner_query.strip().lower()

    def field(u: dict, k: str) -> str:
        v = u.get(k)
        return v.strip().lower() if isinstance(v, str) else ""

    exact = []
    for u in resp:
        if not isinstance(u, dict):
            continue
        if target in (field(u, "email"), field(u, "login"), field(u, "username")):
            exact.append(u)

    candidates = exact if exact else [u for u in resp if isinstance(u, dict)]

    if len(candidates) == 1:
        uid = candidates[0].get("id")
        if isinstance(uid, int):
            return uid
        raise RuntimeError(f"Resolved owner but missing numeric id in user record: {candidates[0]}")

    # Ambiguous: produce a helpful error
    summary = []
    for u in candidates[:10]:
        uid = u.get("id")
        email = u.get("email", "")
        login = u.get("login", "") or u.get("username", "")
        name = " ".join([x for x in [u.get("firstname", ""), u.get("lastname", "")] if x]).strip()
        summary.append(f"id={uid} login={login!s} email={email!s} name={name!s}")

    raise RuntimeError(
        "Owner query is ambiguous; matches:\n  - " + "\n  - ".join(summary)
        + "\nTip: use --owner-id to set it directly, or pass a more specific email/login."
    )


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    base = args.url.rstrip("/")
    token = args.token
    timeout = args.timeout

    internal_bool = True if args.internal == "true" else False

    # Optional dedupe check
    dedupe_marker = ""
    if args.dedupe_key:
        dedupe_marker = f"[dedupe:{args.dedupe_key}]"
        query = urllib.parse.quote(dedupe_marker, safe="")
        search_url = f"{base}/api/v1/tickets/search?query={query}"

        try:
            _, search_resp = http_json("GET", search_url, token, timeout=timeout)
        except ConnectionError as e:
            print(f"Network error during dedupe search: {e}", file=sys.stderr)
            return 5
        except RuntimeError as e:
            print(f"API error during dedupe search: {e}", file=sys.stderr)
            return 4

        if dedupe_marker in json.dumps(search_resp, ensure_ascii=False):
            print(f"Ticket already exists for dedupe key: {args.dedupe_key}")
            return 0

    title = args.title + (f" {dedupe_marker}" if dedupe_marker else "")

    payload: dict = {
        "title": title,
        "group": args.group,
        "customer": args.customer,
        "article": {
            "subject": args.subject,
            "body": args.body,
            "type": args.type,
            "internal": internal_bool,
        },
    }

    # Owner resolution (optional)
    owner_id = 0
    if args.owner_id and args.owner:
        print("Use either --owner or --owner-id, not both.", file=sys.stderr)
        return 2

    if args.owner_id > 0:
        owner_id = args.owner_id
    elif args.owner:
        try:
            owner_id = resolve_owner_id(base, token, args.owner, timeout)
        except ConnectionError as e:
            print(f"Network error resolving owner: {e}", file=sys.stderr)
            return 5
        except RuntimeError as e:
            print(f"API error resolving owner: {e}", file=sys.stderr)
            return 4

    if owner_id > 0:
        payload["owner_id"] = owner_id

    create_url = f"{base}/api/v1/tickets"

    try:
        _, resp = http_json("POST", create_url, token, payload=payload, timeout=timeout)
    except ConnectionError as e:
        print(f"Network error creating ticket: {e}", file=sys.stderr)
        return 5
    except RuntimeError as e:
        print(f"API error creating ticket: {e}", file=sys.stderr)
        return 4

    print(json.dumps(resp, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
