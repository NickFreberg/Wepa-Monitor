"""Backup collectors: a second computer that stands by, covers outages, and hands back cleanly.

    python -m wepa_monitor backup --primary https://<the app> [--id mac]

The handshake (every message signed with WEPA_PEER_KEY, a secret both sides share):

  STANDBY  Every 15 s the backup asks the main app for its status. The answer is signed too, and bound
           to the backup's own one-time number, so it can't be faked or replayed.
  FAILOVER If the main app's collector hasn't recorded a reading for 60 s (or the app doesn't answer),
           the backup starts collecting once a minute. While the main dashboard is up it sends each
           reading there, so the dashboard stays current even though the main collector is down.
  HANDBACK As soon as a status check shows the main collector live again, the backup stops collecting
           (within one 15-second check, well inside 60 s), tells the main app the dates it covered, and
           sends everything it collected. The main app keeps it beside its own data as an import;
           overlapping minutes are de-duplicated, so nothing is counted twice.
  UPDATE   If the main app now runs a newer version, the backup updates itself (git fast-forward and the
           hash-pinned packages) and restarts, so the next failover runs the same code.

The main app logs each takeover and handback in the activity feed and the Software page shows each
backup's last check-in. Signing: HMAC-SHA256 over method, path, time, a one-time number and the body's
SHA-256; messages more than 2 minutes old, or seen before, are refused.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess  # nosec B404 - fixed git/pip commands for self-update
import sys
import threading
import time
from pathlib import Path

import pandas as pd

from . import __version__, config, durations, store

POLL_S = 15
FAILOVER_S = 60
LIVE_S = 150                 # the main collector counts as live if its last good reading is this recent
MAX_SKEW_S = 120
UPLOAD_ROWS = 5000
_ID = re.compile(r"^[a-z0-9]{1,20}$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_seen_nonces: dict[str, float] = {}
_nonce_lock = threading.Lock()


def key() -> bytes | None:
    k = os.environ.get("WEPA_PEER_KEY", "")
    return k.encode() if len(k) >= 32 else None


# --- signing -------------------------------------------------------------------------------------------

def _mac(k: bytes, method: str, path: str, ts: str, nonce: str, body: bytes) -> str:
    msg = f"{method}\n{path}\n{ts}\n{nonce}\n{hashlib.sha256(body).hexdigest()}".encode()
    return hmac.new(k, msg, hashlib.sha256).hexdigest()


def sign(k: bytes, method: str, path: str, body: bytes = b"", nonce: str | None = None) -> dict:
    ts, nonce = str(int(time.time())), nonce or secrets.token_hex(16)
    return {"X-Wepa-Peer-Ts": ts, "X-Wepa-Peer-Nonce": nonce,
            "X-Wepa-Peer-Sig": _mac(k, method, path, ts, nonce, body)}


def verify(k: bytes, method: str, path: str, headers, body: bytes, remember: bool = True) -> bool:
    ts, nonce, sig = (headers.get("X-Wepa-Peer-Ts", ""), headers.get("X-Wepa-Peer-Nonce", ""),
                      headers.get("X-Wepa-Peer-Sig", ""))
    if not (ts.isdigit() and re.fullmatch(r"[0-9a-f]{32}", nonce) and sig):
        return False
    if abs(time.time() - int(ts)) > MAX_SKEW_S:
        return False
    if not hmac.compare_digest(sig, _mac(k, method, path, ts, nonce, body)):
        return False
    if remember:
        with _nonce_lock:
            now = time.time()
            for n, t in list(_seen_nonces.items()):
                if now - t > 2 * MAX_SKEW_S:
                    del _seen_nonces[n]
            if nonce in _seen_nonces:
                return False
            _seen_nonces[nonce] = now
    return True


# --- the main app's side -------------------------------------------------------------------------------

def own_last_ok(data_dir: Path) -> float | None:
    """When this app's own collector last read the Wepa page successfully (imports don't count)."""
    folder = Path(data_dir) / "scrape_log"
    if not folder.exists():
        return None
    for f in sorted(folder.glob("*.csv"), reverse=True)[:2]:
        try:
            with open(f, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - 8192))
                tail = fh.read().decode("utf-8", "replace").splitlines()[1:]
        except OSError:
            continue
        for line in reversed(tail):
            parts = line.split(",")
            if len(parts) >= 2 and parts[1].strip().lower() in ("true", "1"):
                try:
                    return pd.Timestamp(parts[0]).timestamp()
                except ValueError:
                    continue
    return None


def status(data_dir: Path) -> dict:
    from . import updates
    last = own_last_ok(data_dir)
    live = last is not None and time.time() - last <= LIVE_S
    collecting = os.environ.get("WEPA_COLLECT", "1") == "1"
    return {"role": "primary", "version": __version__, "commit": updates.build_info().get("commit", ""),
            "collector_live": bool(live and collecting), "last_ok": last, "now": time.time()}


def _peers_path(data_dir: Path) -> Path:
    from .refs import records_dir
    return records_dir(data_dir) / "peers.json"


def peers(data_dir: Path) -> dict:
    try:
        return json.loads(_peers_path(data_dir).read_text())
    except (OSError, ValueError):
        return {}


def _note_peer(data_dir: Path, backup_id: str, **fields) -> None:
    from .refs import _FileLock
    p = _peers_path(data_dir)
    with _FileLock(p.with_suffix(".lock")):
        d = peers(data_dir)
        d[backup_id] = {**d.get(backup_id, {}), **fields, "last_seen": time.time()}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, indent=1))


def merge_upload(data_dir: Path, backup_id: str, kind: str, day: str, rows: list[dict]) -> int:
    """Store a backup's readings as imports/<kind>/<day>.backup<id>.parquet, merged with what it sent before."""
    if kind == "snapshots":
        cols, typer = store.SNAPSHOT_COLUMNS, store._typed_snapshots
    else:
        cols, typer = store.LOG_COLUMNS, store._typed_log
    df = pd.DataFrame(rows)
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"missing columns: {', '.join(missing[:5])}")
    df = typer(df[cols].copy())
    ts = "scrape_ts" if kind == "snapshots" else "attempt_ts"
    if not (df[ts].dt.strftime("%Y-%m-%d") == day).all():
        raise ValueError("rows from another day")
    dest = Path(data_dir) / "imports" / kind / f"{day}.backup{backup_id}.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        df = pd.concat([pd.read_parquet(dest), df], ignore_index=True)
    keys = ["station_id", ts] if kind == "snapshots" else [ts]
    df = df.drop_duplicates(keys, keep="last").sort_values(keys, ignore_index=True)
    tmp = dest.with_suffix(".tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(dest)
    return len(df)


def install(server, data_dir: Path) -> None:
    """The main app's endpoints. They answer 404 unless WEPA_PEER_KEY is set (32+ characters)."""
    from flask import Response, request

    from . import security, sysevents

    def reply(obj: dict, status_code: int = 200):
        body = json.dumps(obj).encode()
        k = key()
        headers = sign(k, "RESP", request.path, body, nonce=request.headers.get("X-Wepa-Peer-Nonce")) if k else {}
        return Response(body, status_code, mimetype="application/json", headers=headers)

    def guard():
        k = key()
        if k is None:
            return Response("Not found", 404)
        body = request.get_data(cache=True)
        if not verify(k, request.method, request.path, request.headers, body):
            security.log("refused an unsigned or replayed backup-collector message", "warn")
            return Response(json.dumps({"error": "bad signature"}), 401, mimetype="application/json")
        bid = request.headers.get("X-Wepa-Peer-Id", "")
        if not _ID.match(bid):
            return reply({"error": "bad backup id"}, 400)
        return None

    @server.route("/_peer/status", methods=["GET"])
    def _peer_status():
        bad = guard()
        if bad:
            return bad
        bid = request.headers["X-Wepa-Peer-Id"]
        state = request.args.get("state", "standby")
        _note_peer(data_dir, bid, state=state if state in ("standby", "active") else "standby",
                   version=request.args.get("version", "")[:20])
        return reply(status(data_dir))

    @server.route("/_peer/upload", methods=["POST"])
    def _peer_upload():
        bad = guard()
        if bad:
            return bad
        bid = request.headers["X-Wepa-Peer-Id"]
        try:
            msg = json.loads(request.get_data(cache=True))
            kind, day, rows = msg["kind"], msg["day"], msg["rows"]
            if kind not in ("snapshots", "scrape_log") or not _DAY.match(day) or not isinstance(rows, list) \
                    or len(rows) > UPLOAD_ROWS:
                raise ValueError("bad upload")
            n = merge_upload(data_dir, bid, kind, day, rows)
        except (ValueError, KeyError, TypeError) as exc:
            return reply({"error": str(exc)[:200]}, 400)
        _note_peer(data_dir, bid, last_upload=time.time())
        return reply({"ok": True, "rows": n})

    @server.route("/_peer/event", methods=["POST"])
    def _peer_event():
        bad = guard()
        if bad:
            return bad
        bid = request.headers["X-Wepa-Peer-Id"]
        try:
            msg = json.loads(request.get_data(cache=True))
            kind = msg["kind"]
            since = float(msg.get("since") or 0)
            until = float(msg.get("until") or time.time())
        except (ValueError, KeyError, TypeError):
            return reply({"error": "bad event"}, 400)
        ver = str(msg.get("version", ""))[:20]
        tz = config.LOCAL_TZ
        fmt = lambda t: pd.Timestamp(t, unit="s", tz="UTC").tz_convert(tz).strftime("%-I:%M %p")  # noqa: E731
        if kind == "active":
            sysevents.add("backup_active", f"Backup collector '{bid}' took over collecting",
                          f"The main collector had no reading since {fmt(since)}; the backup is collecting "
                          "and sending readings here.", href="/software#backups", data_dir=data_dir, ts=since)
            _note_peer(data_dir, bid, state="active", active_since=since)
        elif kind == "handover":
            mins = max(0.0, (until - since) / 60)
            sysevents.add("backup_handover", f"Main collector back; backup '{bid}' stood down",
                          f"The backup covered {fmt(since)} to {fmt(until)} ({durations.minutes(mins)}) and sent its "
                          "readings here.", href="/software#backups", data_dir=data_dir, ts=until)
            _note_peer(data_dir, bid, state="standby", last_cover={"since": since, "until": until})
        elif kind == "updated":
            sysevents.add("backup_updated", f"Backup collector '{bid}' updated to version {ver}",
                          "It now runs the same version as the main app.", href=f"/changelog/{ver}",
                          data_dir=data_dir)
            _note_peer(data_dir, bid, version=ver)
        else:
            return reply({"error": "unknown event"}, 400)
        st = status(data_dir)
        return reply({"ack": True, "version": st["version"], "commit": st["commit"]})


# --- the backup's side ---------------------------------------------------------------------------------

class PrimaryError(Exception):
    pass


class Backup:
    def __init__(self, primary: str, k: bytes, data_dir: Path, backup_id: str, *, session=None,
                 scrape=None, self_update: bool = True, log=print, poll_s: float = POLL_S,
                 failover_s: float = FAILOVER_S):
        if not _ID.match(backup_id):
            raise ValueError("backup id: 1-20 lowercase letters and digits")
        import requests
        self.primary, self.key, self.data_dir, self.id = primary.rstrip("/"), k, Path(data_dir), backup_id
        self.http = session or requests.Session()
        self.scrape = scrape or (lambda d: __import__("wepa_monitor.collector", fromlist=["x"]).scrape_once(d, quiet=True))
        self.self_update, self.log, self.poll_s, self.failover_s = self_update, log, poll_s, failover_s
        self.state = "standby"
        self.down_since: float | None = None
        self.active_since: float | None = None
        self.next_scrape = 0.0
        self.uploaded_until: float | None = None
        self.last_status: dict | None = None
        self.pending: tuple[float, float] | None = None     # a handback the main app hasn't acknowledged yet

    # transport
    def _call(self, method: str, path: str, obj: dict | None = None, query: str = "") -> dict:
        body = json.dumps(obj).encode() if obj is not None else b""
        headers = {**sign(self.key, method, path, body), "X-Wepa-Peer-Id": self.id,
                   "Content-Type": "application/json"}
        try:
            r = self.http.request(method, self.primary + path + query, data=body or None, headers=headers,
                                  timeout=20, allow_redirects=False)
        except Exception as exc:  # noqa: BLE001 - any transport failure means "not reachable"
            raise PrimaryError(f"unreachable: {type(exc).__name__}") from exc
        resp_body = r.content
        rh = r.headers
        if rh.get("X-Wepa-Peer-Nonce") != headers["X-Wepa-Peer-Nonce"] or \
                not verify(self.key, "RESP", path, rh, resp_body, remember=False):
            raise PrimaryError(f"unsigned or mismatched reply (HTTP {r.status_code})")
        data = json.loads(resp_body)
        if r.status_code >= 400:
            raise PrimaryError(data.get("error", f"HTTP {r.status_code}"))
        return data

    def check(self) -> dict | None:
        try:
            self.last_status = self._call("GET", "/_peer/status",
                                          query=f"?state={self.state}&version={__version__}")
        except PrimaryError as exc:
            self.log(f"main app: {exc}")
            self.last_status = None
        return self.last_status

    # the state machine
    def step(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        st = self.check()
        healthy = bool(st and st.get("collector_live"))
        if self.state == "standby":
            if healthy:
                self.down_since = None
                if self.pending:
                    self._send_handback(*self.pending)
                self.maybe_update(st)
            else:
                self.down_since = self.down_since or now
                if now - self.down_since >= self.failover_s:
                    self.activate(now, reachable=st is not None)
        else:
            if healthy:
                self.handback(now, st)
            elif now >= self.next_scrape:
                self.scrape(self.data_dir)
                self.next_scrape = (now // 60 + 1) * 60
                if st is not None:
                    self.upload(self.uploaded_until or self.active_since, now)
        return self.state

    def activate(self, now: float, reachable: bool) -> None:
        self.state, self.active_since, self.next_scrape = "active", self.down_since, 0.0
        self.log(f"main collector silent for {now - self.down_since:.0f} s: backup collecting")
        if reachable:
            try:
                self._call("POST", "/_peer/event", {"kind": "active", "since": self.active_since,
                                                     "version": __version__})
            except PrimaryError as exc:
                self.log(f"couldn't tell the main app: {exc}")

    def handback(self, now: float, st: dict) -> None:
        # Stop collecting first: this is the "kill within 60 s" guarantee, before anything slow.
        self.state = "standby"
        since, self.down_since = self.active_since, None
        self.log("main collector live again: backup stopped collecting")
        self.active_since = None
        if self._send_handback(since, now):
            self.maybe_update(st)

    def _send_handback(self, since: float, until: float) -> bool:
        try:
            self._call("POST", "/_peer/event", {"kind": "handover", "since": since, "until": until,
                                                "version": __version__})
            n = self.upload(since, until)
        except PrimaryError as exc:
            self.log(f"handback not finished ({exc}); retrying with the next check")
            self.pending = (since, until)
            return False
        self.pending, self.uploaded_until = None, None
        self.log(f"handback acknowledged; {n:,} readings sent")
        return True

    def upload(self, since: float | None, until: float) -> int:
        """Send local readings from since..until, day by day, in chunks the main app accepts."""
        if since is None:
            return 0
        sent = 0
        start, end = pd.Timestamp(since, unit="s", tz="UTC"), pd.Timestamp(until, unit="s", tz="UTC")
        full_log = store.load_scrape_log(self.data_dir)
        for day in pd.date_range(start.normalize(), end.normalize(), freq="D"):
            d = day.strftime("%Y-%m-%d")
            snaps = store.load_day(self.data_dir, d)
            snaps = snaps[(snaps["scrape_ts"] >= start - pd.Timedelta(minutes=1)) & (snaps["scrape_ts"] <= end)]
            log = full_log[(full_log["attempt_ts"] >= start - pd.Timedelta(minutes=1)) & (full_log["attempt_ts"] <= end)
                           & (full_log["attempt_ts"].dt.strftime("%Y-%m-%d") == d)]
            for kind, df, cols in (("snapshots", snaps, store.SNAPSHOT_COLUMNS), ("scrape_log", log, store.LOG_COLUMNS)):
                if df.empty:
                    continue
                out = df[cols].copy()
                for c in out.columns:
                    if str(out[c].dtype).startswith("datetime"):
                        out[c] = out[c].map(lambda t: t.isoformat())
                    elif str(out[c].dtype) == "category":
                        out[c] = out[c].astype(str)
                recs = json.loads(out.to_json(orient="records"))
                for i in range(0, len(recs), UPLOAD_ROWS):
                    self._call("POST", "/_peer/upload", {"kind": kind, "day": d, "rows": recs[i:i + UPLOAD_ROWS]})
                    sent += len(recs[i:i + UPLOAD_ROWS])
        self.uploaded_until = until
        return sent

    # staying current
    def maybe_update(self, st: dict | None) -> bool:
        from . import updates
        if not st or not self.self_update or self.state != "standby":
            return False
        theirs, mine = st.get("version", ""), __version__
        if not updates.valid_version(theirs) or updates.version_key(theirs) <= updates.version_key(mine):
            return False
        self.log(f"main app runs {theirs}, this backup {mine}: updating")
        if not self._update_code(st.get("commit", "")):
            return False
        try:
            self._call("POST", "/_peer/event", {"kind": "updated", "version": theirs})
        except PrimaryError:
            pass
        self._restart()
        return True

    def _run(self, cmd: list[str]) -> bool:
        root = Path(__file__).resolve().parent.parent
        r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=900)  # nosec B603
        if r.returncode:
            self.log(f"{' '.join(cmd[:3])} failed: {(r.stderr or r.stdout).strip()[:200]}")
        return r.returncode == 0

    def _update_code(self, commit: str) -> bool:
        if not (Path(__file__).resolve().parent.parent / ".git").exists():
            self.log("not a git checkout: update this backup by hand (git pull)")
            return False
        if not self._run(["git", "pull", "--ff-only"]):
            return False
        if commit and re.fullmatch(r"[0-9a-f]{7,40}", commit):
            self._run(["git", "merge", "--ff-only", commit])
        pip = [sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check"]
        return self._run(pip + ["--require-hashes", "-r", "requirements.lock"]) or \
            self._run(pip + ["-r", "requirements.txt"])

    def _restart(self) -> None:
        self.log("restarting on the new version")
        os.execv(sys.executable, [sys.executable, "-m", "wepa_monitor", *sys.argv[1:]])  # nosec B606

    def run(self) -> None:
        self.log(f"backup '{self.id}' standing by for {self.primary}; checks every {self.poll_s:.0f} s, takes "
                 f"over after {self.failover_s:.0f} s without a reading")
        while True:
            t0 = time.time()
            try:
                self.step(t0)
            except Exception as exc:  # noqa: BLE001 - the backup must keep running
                self.log(f"backup error: {type(exc).__name__}: {exc}")
            time.sleep(max(1.0, self.poll_s - (time.time() - t0)))


def add_parser(sub) -> None:
    p = sub.add_parser("backup", help="stand by as a backup collector for the main app")
    p.add_argument("--primary", default=os.environ.get("WEPA_URL", ""), help="the main app's address (or WEPA_URL)")
    p.add_argument("--id", default="", help="this backup's name, letters and digits (default: this computer's name)")
    p.add_argument("--no-self-update", action="store_true", help="don't update this copy to the main app's version")


def run_cli(args) -> int:
    k = key()
    if k is None:
        sys.exit("Set WEPA_PEER_KEY (the same 32+ character secret the main app has).")
    url = args.primary.rstrip("/")
    if not url:
        sys.exit("Give the main app's address with --primary https://… (or set WEPA_URL).")
    if url.startswith("http://") and not any(h in url for h in ("://localhost", "://127.0.0.1")):
        sys.exit("Refusing plain http: use the https:// address.")
    import socket
    bid = args.id or re.sub(r"[^a-z0-9]", "", socket.gethostname().lower())[:20] or "backup"
    data_dir = args.data_dir or config.LIVE_DATA_DIR.parent / "backup"
    try:
        Backup(url, k, data_dir, bid, self_update=not args.no_self_update,
               log=lambda m: print(f"{time.strftime('%H:%M:%S')}  {m}", flush=True)).run()
    except KeyboardInterrupt:
        print("stopped")
    return 0
