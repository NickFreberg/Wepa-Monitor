"""Outage risk: the chance each printer goes down in the next 24 hours, learned from its own history.

How it stays honest
-------------------
* Every example is "this printer, at this hour, knowing only what had happened before that hour";
  the label is whether a NEW outage started in the following 24 hours. Hours when the printer was
  already down, or not being monitored, are left out.
* Candidates are compared walk-forward: for each of the last four weeks, train only on what came
  before it (with a 24-hour gap so no test outage leaks into training) and predict that week. That
  mimics real use, predicting a future the model hasn't seen, and copes with the campus changing
  season to season. Recent history is weighted more (half-life three weeks).
* The yardstick is a simple baseline: each printer's own past outage rate. A model has to beat it
  (Brier skill score) and separate risky from safe hours (ROC AUC) before it is shown.
* Until there is enough history (days, outages to learn from and to test on) the status is
  "learning" and the dashboard says so instead of guessing.
* Every prediction shown is logged; a day later the log is compared with what actually happened, so
  the page can show the model's live track record, not just its back-test.

Candidates: logistic regression, gradient-boosted trees, and a small neural network (a multi-layer
perceptron). The best on the held-out period wins, refitted on all the data, retrained nightly.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, metrics as M

HORIZON_H = 24
WARMUP_H = 24                 # first day of history has no "past" to learn from
GAP_H = HORIZON_H             # between train and test, so labels can't leak
FOLDS = 4                     # walk-forward test weeks
HALF_LIFE_D = 21.0            # recency weighting: history this many days old counts half
PRIOR_DAYS = 7.0              # shrinkage strength for each printer's own outage rate
STALE_H = 26                  # retrain when the saved model is older than this

# The go-live gate. All must hold on the held-out period.
GATE = {"min_days": 21, "min_train_pos": 50, "min_test_pos": 25, "min_auc": 0.65, "min_skill": 0.02}

FEATURES = {
    # name: (group, plain-English description)
    "hour_sin": ("time", "time of day"), "hour_cos": ("time", "time of day"),
    "dow_sin": ("time", "day of week"), "dow_cos": ("time", "day of week"), "weekend": ("time", "weekend"),
    "desk_open": ("desk", "support desk open"), "desk_next_open_h": ("desk", "hours until the desk opens"),
    "classes": ("calendar", "classes in session"),
    "residence": ("place", "residence-hall printer"),
    "past_rate": ("history", "its long-run outage rate"), "red_30d": ("history", "outages in the last month"),
    "red_24h": ("recent", "outages in the last day"), "red_7d": ("recent", "outages in the last week"),
    "since_red_h": ("recent", "time since its last outage"),
    "yellow_now": ("warning", "a warning showing now"), "yellow_24h": ("warning", "warnings in the last day"),
    "faults_7d": ("faults", "printer faults in the last week"), "jams_7d": ("faults", "paper jams in the last week"),
    "paper_7d": ("faults", "paper problems in the last week"),
    "toner_min": ("supplies", "its lowest toner"), "drum_min": ("supplies", "its most worn drum"),
    "fuser": ("supplies", "fuser life"), "belt": ("supplies", "belt life"),
    "use_24h": ("use", "printing in the last day"), "use_7d": ("use", "printing in the last week"),
}
GROUP_PHRASE = {"time": "the time of day and week", "desk": "the support desk being closed",
                "calendar": "the academic calendar", "place": "the kind of location",
                "history": "its own track record", "recent": "recent outages", "warning": "a current warning",
                "faults": "recent faults (jams, paper)", "supplies": "worn or low supplies", "use": "heavy use"}
MODEL_SHORT = {"baseline": "Baseline", "logistic": "Logistic regression", "boosted": "Boosted trees",
               "neural": "Neural network"}
MODEL_NAMES = {"baseline": "Its own past rate (baseline)", "logistic": "Logistic regression",
               "boosted": "Gradient-boosted trees", "neural": "Neural network (multi-layer perceptron)"}


# --- features ------------------------------------------------------------------------------------

def _hourly_levels(ds: M.Dataset) -> pd.DataFrame:
    """Station x hour: last reading of each consumable before the hour, and toner used in the hour."""
    c = ds.cons
    if c.empty:
        return pd.DataFrame()
    h = c["scrape_ts"].dt.ceil("h")
    lv = c.assign(h=h).groupby(["station_id", "h", "component"])["level"].last().unstack("component")
    use = c[c["component"] == "toner_k"].assign(h=h[c["component"] == "toner_k"]).groupby(
        ["station_id", "h"])["used"].sum().rename("used")
    return lv.join(use, how="outer")


def _starts(df: pd.DataFrame, sid: str) -> np.ndarray:
    return np.sort(df.loc[df["station_id"] == sid, "start"].values.astype("datetime64[ns]").astype(np.int64))


def _intervals(df: pd.DataFrame, sid: str, as_of) -> tuple[np.ndarray, np.ndarray]:
    d = df[df["station_id"] == sid].sort_values("start")
    end = d["end"].fillna(as_of)
    return (d["start"].values.astype("datetime64[ns]").astype(np.int64),
            end.values.astype("datetime64[ns]").astype(np.int64))


def _count(starts: np.ndarray, t: np.ndarray, window_h: float) -> np.ndarray:
    return np.searchsorted(starts, t, "left") - np.searchsorted(starts, t - int(window_h * 3.6e12), "left")


def _covering(starts, ends, t) -> np.ndarray:
    i = np.searchsorted(starts, t, "right") - 1
    ok = i >= 0
    out = np.zeros(len(t), dtype=bool)
    out[ok] = ends[i[ok]] > t[ok]
    return out


def build(ds: M.Dataset, hours: pd.DatetimeIndex | None = None, ids=None) -> pd.DataFrame:
    """One row per (station, hour): features known at that hour, plus the label when it's knowable."""
    from . import campus, support
    if ds.empty or ds.data_start is None:
        return pd.DataFrame()
    start = ds.data_start.ceil("h") + pd.Timedelta(hours=WARMUP_H)
    if hours is None:
        hours = pd.date_range(start, ds.as_of.floor("h"), freq="h")
    if len(hours) == 0:
        return pd.DataFrame()
    t = hours.values.astype("datetime64[ns]").astype(np.int64)
    as_of = ds.as_of
    red = ds.sev_inc[ds.sev_inc["severity"] == "red"]
    yel = ds.sev_inc[ds.sev_inc["severity"] == "yellow"]
    fi = ds.fault_inc
    jams = fi[fi["code"].str.contains("jam", na=False)]
    paper = fi[fi["code"].str.contains("paper|tray", na=False) & ~fi["code"].str.contains("jam", na=False)]
    levels = _hourly_levels(ds)
    covered = ds.hourly.assign(h=ds.hourly["hour"] + pd.Timedelta(hours=1)).set_index(["station_id", "h"])["covered_s"]

    local = hours.tz_convert(config.LOCAL_TZ)
    hr, dow = local.hour.values, local.dayofweek.values
    cal = campus.load()
    classes = (np.array([cal.days["phase"].get(d, "") == "classes" for d in local.date], dtype=float)
               if not cal.empty else np.zeros(len(hours)))
    desk_grid = {o: support.grid(o) for o in support.TEAMS}

    def next_open(o):
        g = desk_grid[o]
        out = np.zeros(len(hours))
        for k, (d0, h0) in enumerate(zip(dow, hr)):
            for step in range(0, 24 * 7):
                d, h = (d0 + (h0 + step) // 24) % 7, (h0 + step) % 24
                if g[d, h] > 0:
                    out[k] = step
                    break
            else:
                out[k] = 168
        return out
    nxt = {o: next_open(o) for o in desk_grid}
    first_ns = ds.data_start.value
    # Campus outage rate so far (per printer-day), the prior each printer's own rate is shrunk toward.
    all_red = np.sort(red["start"].values.astype("datetime64[ns]").astype(np.int64))
    days_seen = np.maximum((t - first_ns) / 8.64e13, 1.0)
    campus_rate = (np.searchsorted(all_red, t, "left") + 1.0) / (max(len(ds.stations), 1) * days_seen)

    st = ds.stations if ids is None else ds.stations[ds.stations["station_id"].isin(ids)]
    frames = []
    for _, s in st.iterrows():
        sid = s["station_id"]
        owner = support.owner(s["section"])
        rs = _starts(red, sid)
        r_on, r_off = _intervals(red, sid, as_of)
        y_on, y_off = _intervals(yel, sid, as_of)
        f = pd.DataFrame(index=hours)
        f["station_id"] = sid
        f["hour_sin"], f["hour_cos"] = np.sin(2 * np.pi * hr / 24), np.cos(2 * np.pi * hr / 24)
        f["dow_sin"], f["dow_cos"] = np.sin(2 * np.pi * dow / 7), np.cos(2 * np.pi * dow / 7)
        f["weekend"] = (dow >= 5).astype(float)
        g = desk_grid.get(owner)
        f["desk_open"] = g[dow, hr] if g is not None else 0.0
        f["desk_next_open_h"] = nxt[owner] if owner in nxt else 168.0
        f["classes"] = classes
        f["residence"] = float(s["station_type"] == "residence")
        # Outages per day so far, shrunk toward the campus rate with PRIOR_DAYS of pseudo-history, so
        # one early outage doesn't make a printer look like the worst on campus.
        f["past_rate"] = (np.searchsorted(rs, t, "left") + PRIOR_DAYS * campus_rate) / (days_seen + PRIOR_DAYS)
        f["red_30d"] = _count(rs, t, 24 * 30)
        f["red_24h"], f["red_7d"] = _count(rs, t, 24), _count(rs, t, 168)
        ends = np.sort(r_off)
        i = np.searchsorted(ends, t, "left") - 1
        last_end = np.where(i >= 0, ends[np.maximum(i, 0)], first_ns) if len(ends) else np.full(len(t), first_ns)
        f["since_red_h"] = np.minimum((t - last_end) / 3.6e12, 24 * 14)
        f["yellow_now"] = _covering(y_on, y_off, t).astype(float)
        f["yellow_24h"] = _count(np.sort(y_on), t, 24)
        f["faults_7d"] = _count(_starts(fi, sid), t, 168)
        f["jams_7d"] = _count(_starts(jams, sid), t, 168)
        f["paper_7d"] = _count(_starts(paper, sid), t, 168)
        if len(levels) and sid in levels.index.get_level_values(0):
            lv = levels.loc[sid].sort_index()
            lv = lv.reindex(lv.index.union(hours)).sort_index()
            used = lv["used"].fillna(0.0) if "used" in lv else pd.Series(0.0, index=lv.index)
            lvl = lv.drop(columns=["used"], errors="ignore").ffill().reindex(hours)
            cs = used.cumsum()
            cs_t = cs.reindex(hours)
            f["use_24h"] = (cs_t - cs.reindex(hours - pd.Timedelta(hours=24), method="ffill").fillna(0).values).values
            f["use_7d"] = (cs_t - cs.reindex(hours - pd.Timedelta(hours=168), method="ffill").fillna(0).values).values
            tk = [c for c in lvl.columns if c.startswith("toner")]
            dr = [c for c in lvl.columns if c.startswith("drum")]
            f["toner_min"] = lvl[tk].min(axis=1).values if tk else np.nan
            f["drum_min"] = lvl[dr].min(axis=1).values if dr else np.nan
            f["fuser"] = lvl["fuser"].values if "fuser" in lvl else np.nan
            f["belt"] = lvl["belt"].values if "belt" in lvl else np.nan
        else:
            for c in ("use_24h", "use_7d", "toner_min", "drum_min", "fuser", "belt"):
                f[c] = np.nan
        # Leave out hours it was already down, or not monitored in the hour before.
        f["down_now"] = _covering(r_on, r_off, t)
        cov = covered.reindex(pd.MultiIndex.from_arrays([[sid] * len(hours), hours])).values
        f["monitored"] = np.nan_to_num(cov.astype(float)) > 0
        nxt_start = rs[np.minimum(np.searchsorted(rs, t, "right"), max(len(rs) - 1, 0))] if len(rs) else None
        has_next = (np.searchsorted(rs, t, "right") < len(rs)) if len(rs) else np.zeros(len(t), dtype=bool)
        f["label"] = (has_next & (nxt_start - t <= HORIZON_H * 3.6e12)).astype(float) if len(rs) else 0.0
        f["labelled"] = t + HORIZON_H * 3.6e12 <= as_of.value
        frames.append(f)
    out = pd.concat(frames).rename_axis("ts").reset_index()
    return out[out["monitored"] & ~out["down_now"]].drop(columns=["monitored", "down_now"]).reset_index(drop=True)


# --- models ------------------------------------------------------------------------------------------

def _candidates():
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.neural_network import MLPClassifier
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    imp = lambda: SimpleImputer(strategy="median", keep_empty_features=True)  # noqa: E731
    return {
        "logistic": make_pipeline(imp(), StandardScaler(), LogisticRegression(max_iter=2000, C=0.5)),
        "boosted": HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_leaf_nodes=15,
                                                  l2_regularization=1.0, random_state=0),
        # Small and strongly regularized: with tens of thousands of rows and weak signals, a bigger
        # network only learns to be overconfident (settings chosen on an earlier, separate week).
        "neural": make_pipeline(imp(), StandardScaler(), MLPClassifier(
            hidden_layer_sizes=(16,), alpha=0.1, learning_rate_init=1e-3, max_iter=200, random_state=0)),
    }


def _weights(ts: pd.Series, until: pd.Timestamp) -> np.ndarray:
    """Recent history counts more: weight halves every HALF_LIFE_D days back from `until`."""
    age_d = (until - ts).dt.total_seconds().values / 86400
    return 0.5 ** (np.maximum(age_d, 0) / HALF_LIFE_D)


def _fit(model, X, y, w):
    """Fit with sample weights, whether the model is a bare estimator or a pipeline."""
    if hasattr(model, "steps"):
        return model.fit(X, y, **{f"{model.steps[-1][0]}__sample_weight": w})
    return model.fit(X, y, sample_weight=w)


def _baseline():
    """Baseline: the printer's own (shrunk) past outage rate, mapped to a 24-hour probability by a
    one-feature logistic fit so it's calibrated like the others."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import FunctionTransformer
    return make_pipeline(FunctionTransformer(lambda x: np.log(np.maximum(x, 1e-4))), LogisticRegression())


def _scores(y, p) -> dict:
    from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
    both = len(np.unique(y)) == 2
    return {"brier": float(brier_score_loss(y, p)), "auc": float(roc_auc_score(y, p)) if both else float("nan"),
            "ap": float(average_precision_score(y, p)) if both else float("nan")}


def _top_k_hit(test: pd.DataFrame, p: np.ndarray, k: int = 3) -> float:
    """At each hour, of the k printers ranked riskiest, the share that really went down within 24 h."""
    d = test.assign(p=p)
    top = d.sort_values("p", ascending=False).groupby("ts").head(k)
    return float(top["label"].mean()) if len(top) else float("nan")


def train(ds: M.Dataset) -> dict:
    import warnings
    from sklearn.exceptions import ConvergenceWarning
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConvergenceWarning)
        return _train(ds)


def _train(ds: M.Dataset) -> dict:
    """Walk-forward evaluation, gate, and a final fit on everything. Returns the model card (and the
    fitted model under '_model').

    Evaluation mirrors real use: for each of the last FOLDS weeks, every candidate is trained only on
    data from before that week (minus a 24-hour gap) and predicts the week. Pooled over the weeks,
    the scores say how the model would have done had it been running then."""
    t0 = time.time()
    X = build(ds)
    lab = X[X["labelled"]] if len(X) else X
    feats = list(FEATURES)
    card = {"trained_at": pd.Timestamp.now(tz="UTC").isoformat(), "data_as_of": ds.as_of.isoformat(),
            "horizon_h": HORIZON_H, "demo": bool(ds.is_demo), "features": feats, "gate": GATE,
            "folds": FOLDS, "half_life_d": HALF_LIFE_D,
            "rows": int(len(lab)), "positives": int(lab["label"].sum()) if len(lab) else 0,
            "days": float((lab["ts"].max() - lab["ts"].min()).total_seconds() / 86400) if len(lab) else 0.0,
            "status": "learning", "reasons": [], "models": {}, "champion": None}

    def done(reason=None):
        if reason:
            card["reasons"].append(reason)
        card["seconds"] = time.time() - t0
        return card
    if len(lab) < 200 or card["positives"] < 10:
        return done("Not enough history yet to learn from.")
    end = lab["ts"].max() + pd.Timedelta(hours=1)
    weeks = [(end - pd.Timedelta(days=7 * (k + 1)), end - pd.Timedelta(days=7 * k)) for k in range(FOLDS)][::-1]
    weeks = [(a, b) for a, b in weeks if (lab["ts"] < a - pd.Timedelta(hours=GAP_H)).sum() >= 200]
    if not weeks:
        return done("Not enough history yet for a fair test (needs at least a week to learn from before a week "
                    "to test on).")
    first_train = lab[lab["ts"] < weeks[0][0] - pd.Timedelta(hours=GAP_H)]
    card.update(test_from=weeks[0][0].isoformat(), train_pos=int(first_train["label"].sum()))
    names = ["baseline"] + list(_candidates())
    preds = {n: [] for n in names}
    errors = {}
    tests = []
    for a, b in weeks:
        tr = lab[lab["ts"] < a - pd.Timedelta(hours=GAP_H)]
        te = lab[(lab["ts"] >= a) & (lab["ts"] < b)]
        if tr["label"].nunique() < 2 or te.empty:
            continue
        w = _weights(tr["ts"], a)
        tests.append(te)
        models = {"baseline": _baseline(), **_candidates()}
        for n, m in models.items():
            cols = ["past_rate"] if n == "baseline" else feats
            try:
                _fit(m, tr[cols].values, tr["label"].values, w)
                preds[n].append(m.predict_proba(te[cols].values)[:, 1])
            except Exception as exc:  # noqa: BLE001 - one failed candidate shouldn't stop the others
                errors[n] = f"{type(exc).__name__}: {exc}"[:200]
                preds[n].append(np.full(len(te), np.nan))
    if not tests:
        return done("The history has no outages to learn from yet.")
    te = pd.concat(tests)
    y = te["label"].values
    card.update(test_rows=int(len(te)), test_pos=int(y.sum()), base_rate=float(y.mean()))
    if len(np.unique(y)) < 2:
        return done("The test weeks had no outages to check predictions against.")
    pooled = {n: np.concatenate(v) for n, v in preds.items()}
    for n, p in pooled.items():
        if n in errors or np.isnan(p).any():
            card["models"][n] = {"error": errors.get(n, "failed on some weeks")}
            continue
        sc = {**_scores(y, p), "top3_hit": _top_k_hit(te, p)}
        card["models"][n] = sc
    base = card["models"].get("baseline", {})
    for n, sc in card["models"].items():
        if n != "baseline" and "brier" in sc and "brier" in base:
            sc["skill"] = 1 - sc["brier"] / base["brier"]
    ok = [n for n in card["models"] if n != "baseline" and "brier" in card["models"][n]]
    if not ok or "brier" not in base:
        return done("No candidate model could be fitted.")
    best = min(ok, key=lambda n: card["models"][n]["brier"])
    card["champion"] = best
    b = card["models"][best]
    reasons = []
    if card["days"] < GATE["min_days"]:
        reasons.append(f"Only {card['days']:.0f} days of history (needs {GATE['min_days']}).")
    if card["train_pos"] < GATE["min_train_pos"]:
        reasons.append(f"Only {card['train_pos']} outages to learn from (needs {GATE['min_train_pos']}).")
    if card["test_pos"] < GATE["min_test_pos"]:
        reasons.append(f"Only {card['test_pos']} outages to test on (needs {GATE['min_test_pos']}).")
    if not b["auc"] >= GATE["min_auc"]:
        reasons.append(f"Its ranking power (AUC {b['auc']:.2f}) is below {GATE['min_auc']}.")
    if not b["skill"] >= GATE["min_skill"]:
        reasons.append(f"It doesn't beat the simple baseline by enough (skill {b['skill']:+.1%}, needs "
                       f"{GATE['min_skill']:+.0%}).")
    card["reasons"] = reasons
    card["status"] = "live" if not reasons else "learning"
    card["calibration"] = _calibration(y, pooled[best])
    # Refit the winner on all labelled history (recent weighted) for today's predictions.
    final = _candidates()[best]
    _fit(final, lab[feats].values, lab["label"].values, _weights(lab["ts"], end))
    card["importance"] = _importance(final, tests[-1], feats)
    card["_model"] = final
    card["_medians"] = lab[feats].median(numeric_only=True).to_dict()
    return done()


def _importance(model, te: pd.DataFrame, feats: list[str]) -> list[dict]:
    """Permutation importance by feature group (how much worse the Brier score gets when that
    information is scrambled), on up to 6,000 held-out rows."""
    from sklearn.metrics import brier_score_loss
    rng = np.random.default_rng(0)
    d = te.sample(min(len(te), 6000), random_state=0)
    X, y = d[feats].values.copy(), d["label"].values
    base = brier_score_loss(y, model.predict_proba(X)[:, 1])
    groups: dict[str, list[int]] = {}
    for i, f in enumerate(feats):
        groups.setdefault(FEATURES[f][0], []).append(i)
    out = []
    for g, cols in groups.items():
        loss = []
        for _ in range(3):
            Xp = X.copy()
            perm = rng.permutation(len(Xp))
            Xp[:, cols] = Xp[perm][:, cols]
            loss.append(brier_score_loss(y, model.predict_proba(Xp)[:, 1]) - base)
        out.append({"group": g, "label": GROUP_PHRASE[g], "importance": float(np.mean(loss))})
    return sorted(out, key=lambda r: -r["importance"])


def _calibration(y, p, bins: int = 8) -> list[dict]:
    q = np.unique(np.quantile(p, np.linspace(0, 1, bins + 1)))
    idx = np.clip(np.searchsorted(q, p, "right") - 1, 0, len(q) - 2)
    return [{"predicted": float(p[idx == k].mean()), "actual": float(y[idx == k].mean()), "n": int((idx == k).sum())}
            for k in range(len(q) - 1) if (idx == k).any()]


# --- storage, prediction, track record --------------------------------------------------------------

def _dir(ds: M.Dataset) -> Path:
    d = Path(ds.data_dir) / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save(ds: M.Dataset, card: dict) -> None:
    import joblib
    d = _dir(ds)
    public = {k: v for k, v in card.items() if not k.startswith("_")}
    (d / "risk_card.json").write_text(json.dumps(public, indent=1, default=float))
    if "_model" in card:
        joblib.dump({"model": card["_model"], "medians": card["_medians"], "features": card["features"],
                     "champion": card["champion"]}, d / "risk_model.joblib")


def load_card(ds: M.Dataset) -> dict | None:
    p = Path(ds.data_dir) / "models" / "risk_card.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _load_model(ds: M.Dataset):
    import joblib
    p = Path(ds.data_dir) / "models" / "risk_model.joblib"
    key = ("risk_model", p.stat().st_mtime if p.exists() else None)
    if key[1] is None:
        return None
    return M.memo(ds, key, lambda: joblib.load(p))


def train_and_save(ds: M.Dataset, log=print) -> dict:
    card = train(ds)
    save(ds, card)
    log(f"Outage-risk model: {card['status']}"
        + (f", {MODEL_NAMES.get(card['champion'], card['champion'])}" if card.get("champion") else "")
        + (f" ({'; '.join(card['reasons'])})" if card["reasons"] else "") + f" in {card.get('seconds', 0):.1f} s")
    return card


_training = threading.Lock()


def ensure_fresh(ds: M.Dataset, log=print, background: bool = True) -> None:
    """Retrain when the saved model is missing or more than STALE_H hours old (in a thread)."""
    card = load_card(ds)
    if card and pd.Timestamp.now(tz="UTC") - pd.Timestamp(card["trained_at"]) < pd.Timedelta(hours=STALE_H):
        return
    if not _training.acquire(blocking=False):
        return

    def run():
        try:
            train_and_save(ds, log)
        except Exception as exc:  # noqa: BLE001 - never take the app down for the model
            log(f"Outage-risk training failed: {type(exc).__name__}: {exc}")
        finally:
            _training.release()
    if background:
        threading.Thread(target=run, name="risk-train", daemon=True).start()
    else:
        run()


def training_now() -> bool:
    return _training.locked()


@dataclass
class Forecast:
    card: dict | None
    table: pd.DataFrame          # station_id, label, building, p, band, reasons (current hour)

    @property
    def live(self) -> bool:
        return bool(self.card and self.card.get("status") == "live")


def predict_now(ds: M.Dataset, ids=None) -> Forecast:
    """Today's risk for every printer that is up, with the main reasons; empty while learning."""
    card = load_card(ds)
    bundle = _load_model(ds) if card and card.get("champion") else None
    if bundle is None:
        return Forecast(card, pd.DataFrame())
    now = pd.DatetimeIndex([ds.as_of.floor("h")])
    X = build(ds, now, ids)
    if X.empty:
        return Forecast(card, pd.DataFrame())
    feats, model = bundle["features"], bundle["model"]
    p = model.predict_proba(X[feats].values)[:, 1]
    base = card.get("base_rate") or float(np.mean(p))
    reasons = _reasons(model, X, feats, bundle["medians"])
    st = ds.stations.set_index("station_id")
    out = pd.DataFrame({"station_id": X["station_id"].values, "p": p, "reasons": reasons})
    out["label"] = out["station_id"].map(st["label"])
    out["building"] = out["station_id"].map(st["building"])
    out["relative"] = out["p"] / base if base > 0 else np.nan
    out["band"] = np.where((out["p"] >= max(0.25, 2 * base)), "high", np.where(out["p"] >= base, "elevated", "low"))
    out = out.sort_values("p", ascending=False).reset_index(drop=True)
    _log_predictions(ds, out, card)
    return Forecast(card, out)


def _reasons(model, X: pd.DataFrame, feats: list[str], medians: dict) -> list[str]:
    """Local explanation: for each printer, which kinds of information push its risk up the most
    (risk with that group set to typical values vs as it is now)."""
    base = model.predict_proba(X[feats].values)[:, 1]
    groups: dict[str, list[int]] = {}
    for i, f in enumerate(feats):
        groups.setdefault(FEATURES[f][0], []).append(i)
    lift = {}
    for g, cols in groups.items():
        if g == "time":
            continue
        Xt = X[feats].values.copy()
        for c in cols:
            Xt[:, c] = medians.get(feats[c], np.nan)
        lift[g] = base - model.predict_proba(Xt)[:, 1]
    out = []
    for k in range(len(X)):
        top = sorted(((v[k], g) for g, v in lift.items() if v[k] > 0.01), reverse=True)[:2]
        out.append(", ".join(GROUP_PHRASE[g] for _, g in top))
    return out


def _log_predictions(ds: M.Dataset, table: pd.DataFrame, card: dict) -> None:
    """Keep one prediction per printer per hour, to score against what really happens."""
    path = _dir(ds) / "risk_log.parquet"
    ts = ds.as_of.floor("h")
    new = table[["station_id", "p"]].assign(ts=ts, model=card.get("champion"), status=card.get("status"))
    try:
        old = pd.read_parquet(path) if path.exists() else pd.DataFrame()
        if len(old) and (old["ts"] == ts).any():
            return
        pd.concat([old, new], ignore_index=True).tail(200_000).to_parquet(path, index=False)
    except Exception:  # noqa: BLE001 - logging must never break the page
        pass


def track_record(ds: M.Dataset) -> dict | None:
    """How the logged predictions did once their 24 hours had passed."""
    path = Path(ds.data_dir) / "models" / "risk_log.parquet"
    if not path.exists():
        return None
    log = pd.read_parquet(path)
    log = log[log["ts"] + pd.Timedelta(hours=HORIZON_H) <= ds.as_of]
    if log.empty:
        return None
    red = ds.sev_inc[ds.sev_inc["severity"] == "red"]
    hit = []
    for sid, ts in zip(log["station_id"], log["ts"]):
        s = red[(red["station_id"] == sid) & (red["start"] > ts) & (red["start"] <= ts + pd.Timedelta(hours=HORIZON_H))]
        hit.append(len(s) > 0)
    log = log.assign(actual=np.array(hit, dtype=float))
    s = _scores(log["actual"].values, log["p"].values)
    hi = log[log["p"] >= log["p"].quantile(0.8)]
    return {"predictions": int(len(log)), "hours": int(log["ts"].nunique()), "brier": s["brier"], "auc": s["auc"],
            "base_rate": float(log["actual"].mean()), "top20_rate": float(hi["actual"].mean()) if len(hi) else np.nan}
