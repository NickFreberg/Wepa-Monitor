"""`python -m wepa_monitor users …`: manage staff accounts on the running app from your workstation.

    python -m wepa_monitor users add jsmith --name "Jordan Smith" --url https://<your app>
    python -m wepa_monitor users list --url https://<your app>
    python -m wepa_monitor users disable jsmith --url …      (accounts are never deleted)
    python -m wepa_monitor users enable jsmith --url …
    python -m wepa_monitor users reset jsmith --url …        (new password)

It asks for the shared administrator's password (or reads WEPA_ADMIN_PASSWORD) and talks to the app's
admin API over HTTPS. The new user's password is generated and printed once, unless you choose to
type one (--ask-password); hand it over in person or through a password manager, not by email.
--url can also come from WEPA_URL. --local edits the accounts in --data-dir directly instead (for a
copy running on this computer).
"""
from __future__ import annotations

import getpass
import os
import sys

DEFAULT_ADMIN = "bsuresnet"


def add_parser(sub) -> None:
    p = sub.add_parser("users", help="create, list, disable or reset staff accounts on the running app")
    p.add_argument("action", choices=["add", "list", "disable", "enable", "reset"])
    p.add_argument("username", nargs="?")
    p.add_argument("--name", help="display name shown on every change, e.g. 'Jordan Smith' (add)")
    p.add_argument("--role", choices=["staff", "viewer"], default="staff",
                   help="staff can work investigations; viewer can only read (add)")
    p.add_argument("--url", default=os.environ.get("WEPA_URL", ""), help="the app's address (or WEPA_URL)")
    p.add_argument("--admin", default=DEFAULT_ADMIN, help=f"administrator username (default {DEFAULT_ADMIN})")
    p.add_argument("--ask-password", action="store_true", help="type the new password instead of generating one")
    p.add_argument("--local", action="store_true", help="edit accounts in --data-dir directly")


def _new_password(args) -> str:
    from .accounts import MIN_PASSWORD, generate_password
    if not args.ask_password:
        return generate_password()
    while True:
        a = getpass.getpass(f"New password for {args.username} (at least {MIN_PASSWORD} characters): ")
        if a != getpass.getpass("Again: "):
            print("They didn't match; try again.")
            continue
        return a


def _remote(args, method: str, path: str, body: dict | None = None) -> dict:
    import requests
    url = args.url.rstrip("/")
    if not url:
        sys.exit("Give the app's address with --url https://… (or set WEPA_URL).")
    local_host = any(h in url for h in ("://localhost", "://127.0.0.1"))
    if url.startswith("http://") and not local_host:
        sys.exit("Refusing to send the administrator password over plain http; use the https:// address.")
    pw = os.environ.get("WEPA_ADMIN_PASSWORD") or getpass.getpass(f"Password for administrator '{args.admin}': ")
    try:
        r = requests.request(method, url + path, json=body, auth=(args.admin, pw), timeout=30,
                             headers={"X-Wepa-Admin": "1"})
    except requests.RequestException as exc:
        sys.exit(f"Couldn't reach {url}: {exc}")
    if r.status_code == 401:
        sys.exit("Sign-in failed: check the administrator username and password.")
    if r.status_code == 429:
        sys.exit("Too many failed sign-ins from this network; wait 15 minutes and try again.")
    try:
        data = r.json()
    except ValueError:
        sys.exit(f"Unexpected reply ({r.status_code}). Is this the right address, and is the app up to date?")
    if r.status_code >= 400:
        sys.exit(f"Not done: {data.get('error', r.status_code)}")
    return data


def run(args) -> int:
    from . import accounts, config
    need_user = args.action != "list"
    if need_user and not args.username:
        sys.exit(f"'users {args.action}' needs a username.")
    if args.action == "add" and not args.name:
        sys.exit("Give the person's name with --name \"First Last\" (it appears on every change they make).")

    if args.local:
        from pathlib import Path
        d = Path(args.data_dir or config.LIVE_DATA_DIR)
        accounts.configure(d.parent / "security" if d.name == "live" else d / "security")

    def call(action, body=None):
        if args.local:
            if action == "list":
                return {"users": accounts.users()}
            if action == "add":
                return {"user": accounts.create(args.username, body["name"], body["password"], body["role"], by="cli")}
            return {"user": accounts.update(args.username, by="cli", **body)}
        if action == "list":
            return _remote(args, "GET", "/_admin/users")
        if action == "add":
            return _remote(args, "POST", "/_admin/users", {"username": args.username, **body})
        return _remote(args, "POST", f"/_admin/users/{args.username}", body)

    try:
        if args.action == "list":
            rows = call("list")["users"]
            if not rows:
                print("No staff accounts yet. Add one with: users add <username> --name \"First Last\"")
            for u in rows:
                print(f"  {u['username']:<20} {u['name']:<28} {u['role']:<7} {'DISABLED' if u.get('disabled') else 'active'}")
            return 0
        if args.action == "add":
            pw = _new_password(args)
            u = call("add", {"name": args.name, "role": args.role, "password": pw})["user"]
            print(f"Created {u['username']} ({u['name']}, {u['role']}).")
        elif args.action in ("disable", "enable"):
            call(args.action, {"disabled": args.action == "disable"})
            print(f"{args.username} is now {'disabled' if args.action == 'disable' else 'active'}.")
            return 0
        else:   # reset
            pw = _new_password(args)
            call("reset", {"password": pw})
            print(f"Password reset for {args.username}.")
        if not args.ask_password:
            print(f"Their password (shown once; share it in person or via a password manager): {pw}")
        return 0
    except accounts.AccountError as exc:
        sys.exit(f"Not done: {exc}")
