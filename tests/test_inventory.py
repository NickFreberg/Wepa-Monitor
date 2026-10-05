"""Inventory: the ledger, locations, moves, counts and shortfall investigations, receipts with a second
approver, automatic deductions from replacements and refills, weekly paper checks, invoice reading, and the
kiosk key log."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from wepa_monitor import investigations as I, inventory as inv, keys, metrics
from wepa_monitor.ledger import Ledger

FIXTURE = Path(__file__).parent / "fixtures" / "live_first_minutes"
A = {"username": "jsmith", "name": "Jordan Smith", "can_edit": True}
B = {"username": "alee", "name": "Alex Lee", "can_edit": True}


@pytest.fixture()
def ds(tmp_path):
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    return metrics.load(tmp_path, now=datetime(2026, 10, 2, 1, 50, tzinfo=timezone.utc))


def _setup(ds):
    d = ds.data_dir
    sid = ds.stations["station_id"].iloc[0]
    bld = ds.stations.set_index("station_id").at[sid, "building"]
    central = inv.add_location(d, A, "central", "Central storage (Library basement)")
    closet = inv.add_location(d, A, "closet", f"{bld} telecom closet", building=bld)
    rid = inv.draft_receipt(d, A, [{"item": "toner_k", "qty": 6, "to": central},
                                   {"item": "paper", "qty": 40, "to": central}], vendor="CDW", stations=ds.stations)
    inv.approve_receipt(d, B, rid)
    return d, sid, bld, central, closet


def test_ledger_is_tamper_evident(tmp_path):
    led = Ledger(tmp_path, "t")
    led.append(A, "x", {"n": 1})
    led.append(A, "x", {"n": 2})
    assert led.verify()[0]
    text = led.path.read_text().replace('"n": 1', '"n": 9')
    led.path.write_text(text)
    ok, msg = led.verify()
    assert not ok and "changed" in msg


def test_receipts_need_a_second_person(ds):
    d = ds.data_dir
    c = inv.add_location(d, A, "central", "Central")
    rid = inv.draft_receipt(d, A, [{"item": "drum_c", "qty": 2, "to": c}], stations=ds.stations)
    assert rid.startswith("RCV")
    assert inv.state(d)["stock"].get((c, "drum_c"), 0) == 0          # nothing until approved
    with pytest.raises(inv.InventoryError, match="second person"):
        inv.approve_receipt(d, A, rid)
    inv.approve_receipt(d, B, rid)
    assert inv.state(d)["stock"][(c, "drum_c")] == 2
    with pytest.raises(inv.InventoryError):
        inv.approve_receipt(d, B, rid)                                 # once only
    r2 = inv.draft_receipt(d, A, [{"item": "drum_c", "qty": 5, "to": c}], stations=ds.stations)
    inv.reject_receipt(d, B, r2, "Packing slip says 3")
    assert inv.state(d)["stock"][(c, "drum_c")] == 2


def test_quantities(ds):
    assert inv.qty("2.345", "paper") == 2.35
    with pytest.raises(inv.InventoryError, match="whole units"):
        inv.qty(1.5, "toner_k")


def test_moves_counts_and_the_trail(ds):
    d, sid, bld, central, closet = _setup(ds)
    inv.move(d, A, "paper", 10, central, closet, stations=ds.stations)
    inv.move(d, A, "paper", 2.5, closet, f"kiosk:{sid}", stations=ds.stations)
    st = inv.state(d, ds.stations)
    assert st["stock"][(central, "paper")] == 30
    assert st["stock"][(closet, "paper")] == 7.5
    with pytest.raises(inv.InventoryError, match="on record"):
        inv.move(d, A, "toner_k", 99, central, closet, stations=ds.stations)
    t = inv.totals(st).set_index("item")
    assert t.at["paper", "total"] == 40 and t.at["paper", "kiosks"] == 2.5
    trail = inv.trail(d, item="paper")
    assert list(trail["action"][:2]) == ["move", "move"] and trail["to"].iloc[0] == f"kiosk:{sid}"


def test_shortfall_opens_an_inventory_investigation(ds):
    d, sid, bld, central, closet = _setup(ds)
    out = inv.count(d, A, central, {"toner_k": 4, "paper": 39.8}, stations=ds.stations,
                    on_shortfall=lambda name, short: inv.open_incident(d, A, name, short))
    assert out["variance"] == {"toner_k": -2, "paper": -0.2}
    rec = I.get(d, out["incident"])
    assert rec["category"] == "STK" and rec["station_id"] == "" and "2 units of Toner K" in rec["description"]
    assert "paper" not in rec["description"].lower().split("short:")[1].split(".")[0]   # within tolerance
    assert inv.state(d)["stock"][(central, "toner_k")] == 4
    # within tolerance, or a surplus: no investigation
    out = inv.count(d, A, central, {"paper": 40.5}, on_shortfall=lambda *a: pytest.fail("no incident"))
    assert out["incident"] == ""


def test_write_offs_take_the_reasons_sign(ds):
    d, sid, bld, central, closet = _setup(ds)
    inv.adjust(d, A, "toner_k", 1, central, "damaged", "Cartridge cracked in the box")
    inv.adjust(d, A, "toner_k", 1, central, "found", "Behind the shelf")
    inv.adjust(d, A, "toner_k", -2, central, "correction", "Double-counted")
    assert inv.state(d)["stock"][(central, "toner_k")] == 4
    with pytest.raises(inv.InventoryError, match="what happened"):
        inv.adjust(d, A, "toner_k", 1, central, "stolen", " ")


def test_automatic_deductions_follow_the_trail(ds):
    d, sid, bld, central, closet = _setup(ds)
    inv.move(d, A, "toner_k", 1, central, closet, stations=ds.stations)
    t0 = pd.Timestamp(inv.state(d)["started"], unit="s", tz="UTC") + pd.Timedelta(minutes=1)
    ds.repl = pd.DataFrame([{"station_id": sid, "component": "toner_k", "ts": t0, "level_before": 2, "level_after": 100},
                            {"station_id": sid, "component": "toner_k", "ts": t0 + pd.Timedelta(days=1),
                             "level_before": 3, "level_after": 100}])
    ds.tray_inc = pd.DataFrame([{"station_id": sid, "start": t0, "end": t0 + pd.Timedelta(hours=1), "status": "resolved",
                                 "tray": "Tray 1", "duration_s": 3600.0}])
    assert inv.sync(ds) == 3
    assert inv.sync(ds) == 0                                           # idempotent
    st = inv.state(d, ds.stations)
    assert st["stock"][(closet, "toner_k")] == 0                       # the building's closet first...
    assert st["stock"][(central, "toner_k")] == 4                      # ...then central storage
    assert st["stock"][(central, "paper")] == round(40 - inv.DEFAULT_TRAY_REAMS, 2)


def test_no_deductions_before_tracking_starts(ds):
    sid = ds.stations["station_id"].iloc[0]
    ds.repl = pd.DataFrame([{"station_id": sid, "component": "fuser", "ts": pd.Timestamp.now(tz="UTC"),
                             "level_before": 1, "level_after": 100}])
    assert inv.sync(ds) == 0


def test_paper_checks(ds):
    d, sid, bld, central, closet = _setup(ds)
    fri = pd.Timestamp("2026-10-02 16:00", tz="UTC")                   # a Friday
    st = inv.state(d, ds.stations)
    pc = inv.paper_checks(st, fri).set_index("location")
    assert pc.at[central, "status"] == "overdue"                       # never counted
    inv.count(d, A, central, {"paper": 40})
    st = inv.state(d, ds.stations)
    now = pd.Timestamp(st["counted"][(central, "paper")], unit="s", tz="UTC")
    assert inv.paper_checks(st, now).set_index("location").at[central, "status"] == "done this week"
    later = now + pd.Timedelta(days=8)
    assert inv.paper_checks(st, later).set_index("location").at[central, "status"] in ("due Friday", "overdue")


def test_locations_are_editable_and_retire_only_when_empty(ds):
    d, sid, bld, central, closet = _setup(ds)
    inv.update_location(d, A, closet, name="Moved closet", building="Weygand Hall")
    assert inv.state(d)["locations"][closet]["building"] == "Weygand Hall"
    with pytest.raises(inv.InventoryError, match="first"):
        inv.retire_location(d, A, central)
    p = inv.add_location(d, A, "paper", "Shea paper closet", building="Shea Hall")
    inv.retire_location(d, A, p)
    assert not inv.state(d)["locations"][p]["active"]


def test_reading_invoice_text():
    text = """INVOICE NO: 88412
    OKI C711 Toner Cartridge Black 44318604   Qty 3   $189.00
    Image Drum Cyan 44318507                  2       $310.00
    Tree Free copy paper 8.5x11, case         4       $212.00
    Fuser unit 120V                            1       $250.00
    Shipping                                   1       $15.00"""
    lines = inv.parse_invoice_text(text)
    got = {ln["item"]: ln["qty"] for ln in lines}
    assert got == {"toner_k": 3, "drum_c": 2, "paper": 40, "fuser": 1}


def test_read_invoice_without_ai_returns_nothing_for_photos(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("WEPA_AI_API_KEY", raising=False)
    assert inv.read_invoice(b"\x89PNG....", "image/png")["lines"] == []


# --- keys ---------------------------------------------------------------------------------------------------

def test_key_log_and_lost_key_incident(ds):
    d = ds.data_dir
    h = keys.add_holder(d, A, "Sam Rivera", "s1rivera@student.bridgew.edu", "Student worker (RSR)", True, "Shea Hall")
    with pytest.raises(keys.KeyLogError, match="BSU email"):
        keys.add_holder(d, A, "X", "x@gmail.com", "Staff")
    k1 = keys.issue(d, A, h, "regular", "R-12", "2026-09-01")
    with pytest.raises(keys.KeyLogError, match="already out"):
        keys.issue(d, A, h, "regular", "R-12", "2026-09-02")
    keys.return_key(d, A, k1, "2026-09-20")
    k2 = keys.issue(d, A, h, "master", "M-1", "2026-09-21")
    ref = keys.lose_key(d, A, k2, "2026-10-01", "Left lanyard in the dining hall")
    rec = I.get(d, ref)
    assert rec["category"] == "KEY" and "counseling" in rec["description"] and "master" in rec["title"].lower()
    t = keys.table(keys.state(d)).set_index("id")
    assert t.at[k2, "status"] == "lost" and t.at[k2, "keys_lost"] == 1 and t.at[k1, "returned"] == "2026-09-20"
    assert keys.counts(keys.state(d)) == {"out": 0, "master_out": 0, "lost": 1, "holders": 1}
    assert keys.ledger(d).verify()[0]


def test_assistant_gets_bsu_policy_facts_but_never_the_key_log(ds):
    from wepa_monitor import ai
    kb = ai.kb_facts()
    assert any("academic work only" in ln for ln in kb) and any("8 cents" in ln for ln in kb)
    keys.add_holder(ds.data_dir, A, "Private Person", "pp@bridgew.edu", "Staff")
    assert "Private Person" not in ai.facts(ds) and "pp@bridgew.edu" not in ai.facts(ds)
