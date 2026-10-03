"""`python -m wepa_monitor users …`: the administrator's tool for the staff directory, run from your
workstation against the running app.

    users add jsmith --name "Jordan Smith" --email jsmith@bridgew.edu [--role viewer]
                     [--resident yes --hall "Scott Hall"]
    users update jsmith [--name …] [--email …] [--role …] [--resident yes|no] [--hall …]
    users deactivate jsmith         (signs them out everywhere; accounts are never deleted)
    users activate jsmith
    users reset jsmith              (new temporary password; they choose their own at next sign-in)
    users list
    users show jsmith
    users halls                     (the residence halls you can assign)

Add --url https://<the app> (or set WEPA_URL). It asks for the administrator's password (or reads
WEPA_ADMIN_PASSWORD) and talks to the app's admin API over HTTPS. New and reset passwords are generated,
printed once, and temporary. --local edits the accounts in --data-dir directly (a copy on this computer).
"""
from __future__ import annotations

import getpass
import os
import sys

DEFAULT_ADMIN = "bsuresnet"
ACTIONS = ["add", "update", "activate", "deactivate", "reset", "list", "show", "halls", "enable", "disable"]


def add_parser(sub) -> None:
    p = sub.add_parser("users", help="manage the staff directory on the running app (administrator)")
    p.add_argument("action", choices=ACTIONS)
    p.add_argument("username", nargs="?")
    p.add_argument("--name", help="full name, shown on every change they make")
    p.add_argument("--email", help="their email address")
    p.add_argument("--role", choices=["staff", "viewer"], help="staff can work investigations; viewer reads only")
    p.add_argument("--resident", choices=["yes", "no"], help="is this person a resident student?")
    p.add_argument("--hall", help="home residence hall (with --resident yes)")
    p.add_argument("--url", default=os.environ.get("WEPA_URL", ""), help="the app's address (or WEPA_URL)")
    p.add_argument("--admin", default=DEFAULT_ADMIN, help=f"administrator username (default {DEFAULT_ADMIN})")
    p.add_argument("--local", action="store_true", help="edit accounts in --data-dir directly")


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
                             headers={"X-Wepa-Admin": "1"}, allow_redirects=False)
    except requests.RequestException as exc:
        sys.exit(f"Couldn't reach {url}: {exc}")
    if r.status_code in (401, 403) and "sign-in" in r.text:
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


def _fmt(u: dict) -> str:
    res = f"resident · {u['hall']}" if u.get("resident") else "not a resident"
    return (f"  {u['username']:<16} {u['name']:<24} {u.get('email') or '(no email)':<28} {u['role']:<7} "
            f"{'active' if u.get('active', True) else 'INACTIVE':<9} {res}")


def run(args) -> int:
    from . import accounts, config
    action = {"enable": "activate", "disable": "deactivate"}.get(args.action, args.action)
    if action not in ("list", "halls") and not args.username:
        sys.exit(f"'users {args.action}' needs a username.")
    if action == "add" and not (args.name and args.email):
        sys.exit('Give --name "First Last" and --email (the administrator sets both).')
    if args.local:
        from pathlib import Path
        d = Path(args.data_dir or config.LIVE_DATA_DIR)
        accounts.configure(d.parent / "security" if d.name == "live" else d / "security")

    fields = {}
    for k in ("name", "email", "role", "hall"):
        if getattr(args, k):
            fields[k] = getattr(args, k)
    if args.resident:
        fields["resident"] = args.resident == "yes"

    try:
        if action in ("list", "halls", "show"):
            data = ({"users": accounts.users(), "halls": accounts.residence_halls()} if args.local
                    else _remote(args, "GET", "/_admin/users"))
            if action == "halls":
                print("\n".join(f"  {h}" for h in data["halls"]) or "  (none known)")
                return 0
            rows = data["users"] if action == "list" else [u for u in data["users"] if u["username"] == args.username]
            if action == "show" and not rows:
                sys.exit(f"No account '{args.username}'.")
            if not rows:
                print('No staff accounts yet. Add one with: users add <username> --name "First Last" --email …')
            for u in rows:
                print(_fmt(u))
                if action == "show":
                    print(f"    phone {u.get('phone') or '-'} · photo {'yes' if u.get('photo') else 'no'} · "
                          f"must change password {'yes' if u.get('must_change') else 'no'}")
            return 0
        if action == "add":
            pw = accounts.generate_password()
            body = {"username": args.username, "password": pw, "role": args.role or "staff", **fields}
            u = (accounts.create(args.username, fields["name"], pw, body["role"], by="cli", email=fields["email"],
                                 resident=fields.get("resident", False), hall=fields.get("hall"))
                 if args.local else _remote(args, "POST", "/_admin/users", body)["user"])
            print(f"Created:\n{_fmt(u)}")
            print(f"Temporary password (shown once; share it in person or via a password manager): {pw}")
            print("They'll choose their own password the first time they sign in.")
            return 0
        if action == "reset":
            pw = accounts.generate_password()
            fields = {"password": pw}
        elif action in ("activate", "deactivate"):
            fields = {"active": action == "activate"}
        elif not fields:
            sys.exit("Nothing to update: give --name, --email, --role, --resident or --hall.")
        u = (accounts.admin_update(args.username, by="cli", **fields) if args.local
             else _remote(args, "POST", f"/_admin/users/{args.username}", fields)["user"])
        print(_fmt(u))
        if action == "reset":
            print(f"Temporary password (shown once): {pw}\nThey're signed out everywhere and must choose a new one.")
        if action == "deactivate":
            print("Deactivated and signed out everywhere.")
        return 0
    except accounts.AccountError as exc:
        sys.exit(f"Not done: {exc}")
