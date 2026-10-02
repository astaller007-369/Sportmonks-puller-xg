"""
Sportmonks football data collector (Streamlit).

- Requests fixtures + team statistics + xG from Sportmonks API v3
- Saves every finished league/season request into a SQLite file (persistent, resumable)
- Exports one CSV per league (played + unplayed matches), home columns left, away columns right
- Tackles won % is calculated from tackles and tackles won (not requested from the API)
"""
import io
import json
import os
import re
import sqlite3
import time
import zipfile
from datetime import date, datetime, timedelta

import pandas as pd
import requests
import streamlit as st

BASE = "https://api.sportmonks.com/v3/football"
DB_PATH = os.environ.get("SM_DB_PATH", "data/sportmonks_cache.sqlite")

# --------------------------------------------------------------------------------------
# Leagues: (sportmonks id, country, league name)
# --------------------------------------------------------------------------------------
LEAGUES = [
    (453, "Poland", "Ekstraklasa"),
    (648, "Brazil", "Serie A"),
    (82, "Germany", "Bundesliga"),
    (85, "Germany", "2. Bundesliga"),
    (301, "France", "Ligue 1"),
    (304, "France", "Ligue 2"),
    (462, "Portugal", "Liga Portugal"),
    (564, "Spain", "La Liga"),
    (567, "Spain", "La Liga 2"),
    (72, "Netherlands", "Eredivisie"),
    (636, "Argentina", "Liga Profesional de Futbol"),
    (573, "Sweden", "Allsvenskan"),
    (325, "Greece", "Super League"),
    (591, "Switzerland", "Super League"),
    (1356, "Australia", "A-League Men"),
    (181, "Austria", "Admiral Bundesliga"),
    (486, "Russia", "Premier League"),
    (384, "Italy", "Serie A"),
    (387, "Italy", "Serie B"),
    (271, "Denmark", "Superliga"),
    (672, "Colombia", "Liga BetPlay"),
    (600, "Turkey", "Super Lig"),
    (360, "Republic of Ireland", "Premier Division"),
    (8, "England", "Premier League"),
    (9, "England", "Championship"),
    (968, "Japan", "J1 League"),
    (208, "Belgium", "Pro League"),
    (211, "Belgium", "Challenger Pro League"),
    (501, "Scotland", "Premiership"),
    (444, "Norway", "Eliteserien"),
    (1007, "India", "Indian Super League"),
    (830, "Egypt", "Premier League"),
    (806, "South Africa", "Premier League"),
    (1064, "Thailand", "Thai Premier League"),
    (743, "Mexico", "Liga MX"),
    (779, "United States", "Major League Soccer"),
    (944, "Saudi Arabia", "Pro League"),
    (651, "Brazil", "Serie B"),
    (1034, "South Korea", "K League 1"),
    (12, "England", "League One"),
    (14, "England", "League Two"),
    # IDs were NOT supplied for these two - assumed, the verifier will tell you if they are wrong
    (79, "Netherlands", "Eerste Divisie"),
    (244, "Croatia", "HNL"),
]
ASSUMED_IDS = {79, 244}
LEAGUE_BY_ID = {i: (c, n) for i, c, n in LEAGUES}


def label(lid):
    c, n = LEAGUE_BY_ID[lid]
    return f"{c} – {n} ({lid})" + (" ⚠ id assumed" if lid in ASSUMED_IDS else "")


def league_country(lid):
    c, n = LEAGUE_BY_ID[lid]
    return f"{c} {n}"


COUNTRY_ALIASES = {
    "united states": {"united states", "usa", "united states of america", "us"},
    "republic of ireland": {"ireland", "republic of ireland"},
    "south korea": {"south korea", "korea republic", "republic of korea", "korea"},
    "russia": {"russia", "russian federation"},
    "turkey": {"turkey", "turkiye"},
}

# --------------------------------------------------------------------------------------
# Output columns (home on the left, away on the right for every metric)
# --------------------------------------------------------------------------------------
METRICS = [
    "goals",
    "goalkeeper_saves",
    "shots_on_target",
    "big_chances_created",
    "corner_kicks",
    "accurate_crosses",
    "successful_crosses_percentage",
    "accurate_long_passes",
    "successful_long_passes_percentage",
    "dribbles_percentage",
    "ground_duels_percentage",
    "aerial_duels_percentage",
    "tackles",
    "tackles_won",
    "tackles_won_percentage",  # calculated, never requested
    "xg",
    "xgot",
    "xg_set_play",
    "xg_open_play",
]
PCT_KEYS = {
    "successful_crosses_percentage",
    "successful_long_passes_percentage",
    "dribbles_percentage",
    "ground_duels_percentage",
    "aerial_duels_percentage",
}
BASE_COLS = ["league_country", "season", "date", "team_home", "team_away"]
EXTRA_COLS = []


def csv_columns():
    cols = list(BASE_COLS)
    for m in METRICS:
        cols += [f"{m}_home", f"{m}_away"]
    return cols + EXTRA_COLS


# --------------------------------------------------------------------------------------
# Persistent storage (SQLite)
# --------------------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS fixtures(
  fixture_id INTEGER PRIMARY KEY, league_id INTEGER, season_id INTEGER,
  season_name TEXT, starting_at TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS ix_fx ON fixtures(league_id, season_id);
CREATE TABLE IF NOT EXISTS jobs(
  league_id INTEGER, season_id INTEGER, season_name TEXT, status TEXT,
  matches INTEGER, note TEXT, updated_at TEXT, PRIMARY KEY(league_id, season_id));
CREATE TABLE IF NOT EXISTS league_meta(
  league_id INTEGER PRIMARY KEY, api_name TEXT, api_country TEXT,
  seasons TEXT, fetched_at REAL);
CREATE TABLE IF NOT EXISTS seen_types(
  source TEXT, name TEXT, mapped TEXT, PRIMARY KEY(source, name));
"""


def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def db_size_mb():
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = DB_PATH + suffix
        if os.path.exists(p):
            total += os.path.getsize(p)
    return total / 1024 / 1024


# --------------------------------------------------------------------------------------
# Sportmonks client
# --------------------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class SM:
    def __init__(self, key, on_wait=None):
        self.s = requests.Session()
        self.s.headers["Authorization"] = key.strip()
        self.calls = 0
        self.on_wait = on_wait

    def _sleep(self, secs, why):
        end = time.time() + secs
        while time.time() < end:
            if self.on_wait:
                self.on_wait(int(end - time.time()) + 1, why)
            time.sleep(1)

    def get(self, path, params=None, allow_404=False):
        last = ""
        for attempt in range(6):
            try:
                r = self.s.get(BASE + path, params=params, timeout=90)
            except requests.RequestException as e:
                last = type(e).__name__
                self._sleep(5 * (attempt + 1), "network error, retrying")
                continue
            self.calls += 1
            if r.status_code == 429:
                wait = 60
                try:
                    wait = int(r.json().get("rate_limit", {}).get("resets_in_seconds", 60))
                except Exception:
                    pass
                self._sleep(min(max(wait, 5) + 2, 3700), "rate limit reached")
                continue
            if r.status_code >= 500:
                last = f"server {r.status_code}"
                self._sleep(10 * (attempt + 1), "server error, retrying")
                continue
            try:
                j = r.json()
            except ValueError:
                raise ApiError(r.status_code, "response was not JSON")
            if r.status_code == 404 and allow_404:
                return {"data": [], "message": j.get("message")}
            if r.status_code >= 400:
                raise ApiError(r.status_code, str(j.get("message") or j)[:300])
            rl = j.get("rate_limit") or {}
            if rl.get("remaining") is not None and rl["remaining"] <= 1:
                self._sleep(int(rl.get("resets_in_seconds", 60)) + 2, "rate limit reached")
            return j
        raise ApiError(0, f"gave up after retries ({last})")


# --------------------------------------------------------------------------------------
# Statistic type classification (by name, so we don't depend on guessed type ids)
# --------------------------------------------------------------------------------------
def norm(s):
    return re.sub(r"[^a-z0-9%]+", " ", (s or "").lower()).strip()


XG_BAD = ("prevent", "against", "difference", "faced", "non penalty", "npxg", "per shot", "conceded")
ID_FALLBACK = {34: "corner_kicks", 86: "shots_on_target", 57: "goalkeeper_saves",
               78: "tackles", 580: "big_chances_created", 5304: "xg", 5305: "xgot"}


def classify_name(n):
    if not n:
        return None
    pct = any(w in n for w in ("percentage", "percent", "%", "accuracy"))
    # ---- xG family
    if "xgot" in n or ("expected" in n and "target" in n):
        return None if any(b in n for b in XG_BAD) else "xgot"
    if re.search(r"\bxg\b|expected goals", n):
        if any(b in n for b in XG_BAD):
            return None
        if "set play" in n or "set piece" in n:
            return "xg_set_play"
        if "open play" in n:
            return "xg_open_play"
        return "xg"
    # ---- tackles
    if "tackle" in n:
        if pct:
            return None
        if "won" in n or "success" in n:
            return "tackles_won"
        return "tackles" if n in ("tackles", "total tackles") else None
    if n in ("saves", "goalkeeper saves", "keeper saves"):
        return "goalkeeper_saves"
    if n in ("shots on target", "shots on goal"):
        return "shots_on_target"
    if "big" in n and "chance" in n and "created" in n:
        return "big_chances_created"
    if n in ("corners", "corner kicks", "corners total"):
        return "corner_kicks"
    if "cross" in n:
        if pct:
            return "successful_crosses_percentage"
        return "accurate_crosses" if ("accurate" in n or "successful" in n) else None
    if "long" in n and "pass" in n:
        if pct:
            return "successful_long_passes_percentage"
        return "accurate_long_passes" if ("accurate" in n or "successful" in n) else None
    if "dribble" in n and pct and "past" not in n:
        return "dribbles_percentage"
    if "ground" in n and "duel" in n and pct:
        return "ground_duels_percentage"
    if "aerial" in n and pct:
        return "aerial_duels_percentage"
    return None


def classify(type_obj, type_id):
    for raw in (type_obj.get("developer_name"), type_obj.get("name")):
        k = classify_name(norm(raw))
        if k:
            return k
    if not type_obj:
        return ID_FALLBACK.get(type_id)
    return None


def extract_value(data, key):
    v = data["value"] if isinstance(data, dict) and "value" in data else data
    if isinstance(v, dict):
        if key in PCT_KEYS:
            prefs = ["percentage", "percent"]
        elif key.startswith("accurate"):
            prefs = ["accurate", "successful"]
        elif key == "tackles_won":
            prefs = ["won", "successful"]
        else:
            prefs = []
        for p in prefs + ["total", "count", "value", "all", "average"]:
            if p in v and not isinstance(v[p], (dict, list)):
                v = v[p]
                break
        else:
            return None
    if isinstance(v, str):
        try:
            v = float(v)
        except ValueError:
            return v
    return v


# --------------------------------------------------------------------------------------
# Fixture parsing
# --------------------------------------------------------------------------------------
def parse_fixture(fx, lid, season_name, seen):
    parts = fx.get("participants") or []
    home = next((p for p in parts if (p.get("meta") or {}).get("location") == "home"), None)
    away = next((p for p in parts if (p.get("meta") or {}).get("location") == "away"), None)
    side_by_pid = {}
    if home:
        side_by_pid[home["id"]] = "home"
    if away:
        side_by_pid[away["id"]] = "away"
    state = fx.get("state") or {}
    row = {
        "league_country": league_country(lid),
        "season": season_name,
        "date": (fx.get("starting_at") or "")[:10],
        "starting_at": fx.get("starting_at"),
        "team_home": home.get("name") if home else None,
        "team_away": away.get("name") if away else None,
        "status": state.get("short_name") or state.get("name"),
        "fixture_id": fx.get("id"),
    }
    # goals from the current score
    for sc in fx.get("scores") or []:
        if sc.get("description") == "CURRENT":
            s = sc.get("score") or {}
            if s.get("participant") in ("home", "away") and s.get("goals") is not None:
                row[f"goals_{s['participant']}"] = s["goals"]
    # statistics + expected goals
    xg_rel = fx.get("xgfixture") or fx.get("xGFixture") or []
    for source, items in (("statistics", fx.get("statistics") or []), ("xg", xg_rel)):
        for it in items:
            t = it.get("type") or {}
            shown = t.get("name") or t.get("developer_name") or f"type_id {it.get('type_id')}"
            key = classify(t, it.get("type_id"))
            seen[(source, shown)] = key or ""
            if not key:
                continue
            side = it.get("location") or side_by_pid.get(it.get("participant_id"))
            if side not in ("home", "away"):
                continue
            if key == "goals":
                continue
            val = extract_value(it.get("data"), key)
            if val is not None:
                row[f"{key}_{side}"] = val
    return row


# --------------------------------------------------------------------------------------
# Data fetching
# --------------------------------------------------------------------------------------
def country_ok(expected, api):
    e, a = norm(expected), norm(api)
    return a == e or a in COUNTRY_ALIASES.get(e, {e})


def ensure_meta(client, lid, max_age=12 * 3600):
    con = db()
    r = con.execute("SELECT api_name, api_country, seasons, fetched_at FROM league_meta WHERE league_id=?",
                    (lid,)).fetchone()
    if r and time.time() - r[3] < max_age:
        con.close()
        return {"api_name": r[0], "api_country": r[1], "seasons": json.loads(r[2])}
    j = client.get(f"/leagues/{lid}", {"include": "country;seasons"})
    d = j.get("data")
    if not d or isinstance(d, list):
        con.close()
        raise ApiError(404, f"league {lid} not returned (check your subscription covers it)")
    seasons = [
        {k: s.get(k) for k in ("id", "name", "starting_at", "ending_at", "is_current")}
        for s in (d.get("seasons") or []) if s.get("starting_at")
    ]
    meta = {"api_name": d.get("name"), "api_country": (d.get("country") or {}).get("name"), "seasons": seasons}
    con.execute("INSERT OR REPLACE INTO league_meta VALUES(?,?,?,?,?)",
                (lid, meta["api_name"], meta["api_country"], json.dumps(seasons), time.time()))
    con.commit()
    con.close()
    return meta


def pick_seasons(seasons, back):
    ss = sorted(seasons, key=lambda s: s["starting_at"], reverse=True)
    if not ss:
        return []
    today = date.today().isoformat()
    cur = next((i for i, s in enumerate(ss) if s.get("is_current")), None)
    if cur is None:
        cur = next((i for i, s in enumerate(ss) if s["starting_at"] <= today), 0)
    return ss[cur: cur + back + 1]


def windows(start, end, step=90):
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=step - 1), end)
        yield cur, nxt
        cur = nxt + timedelta(days=1)


INCLUDE_FULL = "participants;scores;state;statistics.type;xGFixture.type"
INCLUDE_NO_XG = "participants;scores;state;statistics.type"


def fetch_season(client, lid, season, state):
    start = date.fromisoformat(season["starting_at"][:10]) - timedelta(days=3)
    if season.get("ending_at"):
        end = date.fromisoformat(season["ending_at"][:10]) + timedelta(days=3)
    else:
        end = start + timedelta(days=400)
    found, note = {}, None
    for a, b in windows(start, end):
        page = 1
        while True:
            params = {"include": INCLUDE_FULL if state["xg"] else INCLUDE_NO_XG,
                      "filters": f"fixtureLeagues:{lid}", "per_page": 50, "page": page}
            try:
                j = client.get(f"/fixtures/between/{a}/{b}", params, allow_404=True)
            except ApiError as e:
                if state["xg"] and "include" in e.message.lower():
                    state["xg"] = False
                    state["xg_warning"] = ("Your plan rejected the xG include (xGFixture). "
                                           "xG columns will be empty - the xG add-on is needed.")
                    continue
                raise
            for fx in j.get("data") or []:
                if fx.get("league_id") == lid and fx.get("season_id") == season["id"]:
                    found[fx["id"]] = fx
            if not j.get("data") and j.get("message") and not found:
                note = str(j["message"])[:200]
            if (j.get("pagination") or {}).get("has_more"):
                page += 1
            else:
                break
    return list(found.values()), note


def save_job(lid, season, fixtures, note, seen):
    seen_all = {}
    rows = []
    for fx in fixtures:
        row = parse_fixture(fx, lid, season["name"], seen_all)
        rows.append((row["fixture_id"], lid, season["id"], season["name"], row["starting_at"],
                     json.dumps(row, default=str)))
    con = db()
    with con:
        con.execute("DELETE FROM fixtures WHERE league_id=? AND season_id=?", (lid, season["id"]))
        con.executemany("INSERT OR REPLACE INTO fixtures VALUES(?,?,?,?,?,?)", rows)
        con.executemany("INSERT OR REPLACE INTO seen_types VALUES(?,?,?)",
                        [(s, n, m) for (s, n), m in seen_all.items()])
        con.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?,?,?)",
                    (lid, season["id"], season["name"], "done", len(rows), note,
                     datetime.utcnow().isoformat(timespec="seconds")))
    con.close()
    return len(rows)


# --------------------------------------------------------------------------------------
# CSV export
# --------------------------------------------------------------------------------------
def build_df(lid):
    con = db()
    rows = [json.loads(r[0]) for r in con.execute("SELECT data FROM fixtures WHERE league_id=?", (lid,))]
    con.close()
    cols = csv_columns()
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(rows)
    for c in cols + ["starting_at", "fixture_id"]:
        if c not in df.columns:
            df[c] = None
    for side in ("home", "away"):  # tackles won % = tackles won / tackles * 100
        t = pd.to_numeric(df[f"tackles_{side}"], errors="coerce")
        w = pd.to_numeric(df[f"tackles_won_{side}"], errors="coerce")
        df[f"tackles_won_percentage_{side}"] = (w / t * 100).where(t > 0)
    # xG metrics: 2 decimals. Every other metric: whole numbers.
    for m in METRICS:
        for side in ("home", "away"):
            c = f"{m}_{side}"
            num = pd.to_numeric(df[c], errors="coerce")
            if m.startswith("xg"):
                df[c] = num.round(2)
            else:
                df[c] = num.round(0).astype("Int64")
    df = df.sort_values(["starting_at", "fixture_id"], kind="stable")
    return df[cols]


def df_to_csv(df):
    return df.to_csv(index=False).encode("utf-8")


def safe(s):
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")


def build_zip():
    con = db()
    ids = [r[0] for r in con.execute("SELECT DISTINCT league_id FROM fixtures")]
    con.close()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for lid in sorted(ids):
            if lid in LEAGUE_BY_ID:
                z.writestr(f"{safe(league_country(lid))}_{lid}.csv", df_to_csv(build_df(lid)))
    return buf.getvalue(), len(ids)


# --------------------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------------------
st.set_page_config(page_title="Sportmonks Collector", page_icon="⚽", layout="wide")
st.title("⚽ Sportmonks match data collector")

try:
    default_key = st.secrets.get("SPORTMONKS_API_KEY", "")
except Exception:
    default_key = ""

with st.sidebar:
    st.header("Settings")
    api_key = st.text_input("Sportmonks API key", value=default_key, type="password")
    back = st.selectbox("Lookback", list(range(0, 8)), index=1,
                        format_func=lambda n: "Current season only" if n == 0 else f"Current + {n} previous season(s)")
    refresh_latest = st.checkbox("Re-request the current season even if already saved", value=False,
                                 help="Use this to turn unplayed matches into played ones.")
    st.caption(f"Storage file: `{DB_PATH}` · {db_size_mb():.2f} MB")

    with st.expander("Backup / restore storage"):
        st.caption("Streamlit Community Cloud wipes local files on reboot/redeploy. "
                   "Download a backup and restore it later to continue where you left off.")
        if st.button("Prepare backup"):
            con = db()
            tmp = DB_PATH + ".bak"
            dst = sqlite3.connect(tmp)
            con.backup(dst)
            dst.close()
            con.close()
            with open(tmp, "rb") as f:
                st.session_state["backup"] = f.read()
            os.remove(tmp)
        if st.session_state.get("backup"):
            st.download_button("Download backup", st.session_state["backup"],
                               file_name="sportmonks_backup.sqlite", mime="application/x-sqlite3")
        up = st.file_uploader("Restore from backup", type=["sqlite", "db"])
        if up is not None and st.button("Restore now"):
            data = up.getvalue()
            if data[:15] != b"SQLite format 3":
                st.error("Not a valid SQLite backup.")
            else:
                for suffix in ("-wal", "-shm"):
                    if os.path.exists(DB_PATH + suffix):
                        os.remove(DB_PATH + suffix)
                with open(DB_PATH, "wb") as f:
                    f.write(data)
                st.success("Restored.")
                st.rerun()

# ---- league selection
all_ids = [l[0] for l in LEAGUES]
if "sel" not in st.session_state:
    st.session_state["sel"] = []

b1, b2, _ = st.columns([1, 1, 6])
b1.button("Select all", on_click=lambda: st.session_state.update(sel=list(all_ids)))
b2.button("Clear selection", on_click=lambda: st.session_state.update(sel=[]))
sel = st.multiselect("Leagues", options=all_ids, format_func=label, key="sel")

# ---- counters
con = db()
done_ids = {(r[0], r[1]) for r in con.execute("SELECT league_id, season_id FROM jobs WHERE status='done'")}
matches_sel = (con.execute(f"SELECT COUNT(*) FROM fixtures WHERE league_id IN ({','.join('?' * len(sel))})",
                           sel).fetchone()[0] if sel else 0)
matches_all = con.execute("SELECT COUNT(*) FROM fixtures").fetchone()[0]
metas = {r[0]: json.loads(r[1]) for r in con.execute("SELECT league_id, seasons FROM league_meta")}
con.close()

planned = [(lid, s) for lid in sel if lid in metas for s in pick_seasons(metas[lid], back)]
unknown = [lid for lid in sel if lid not in metas]
total_known = len(planned)
done_known = sum(1 for lid, s in planned if (lid, s["id"]) in done_ids)

c1, c2, c3, c4 = st.columns(4)
req_box = c1.empty()
calls_box = c2.empty()
match_box = c3.empty()
stored_box = c4.empty()


def show_counters(done, total, calls, run_matches, matches_selected, extra=""):
    req_box.metric("Requests made", f"{done} / {total}{extra}")
    calls_box.metric("API calls this run", calls)
    match_box.metric("Matches requested (selected leagues)", f"{matches_selected:,}",
                     delta=f"+{run_matches:,} this run" if run_matches else None)
    stored_box.metric("Matches stored (all leagues)", f"{matches_all + run_matches:,}")


show_counters(done_known, total_known, 0, 0, matches_sel,
              extra=f" (+{len(unknown)} league(s) not scanned yet)" if unknown else "")
st.caption("One request = one league-season (all its fixtures, statistics and xG, across however many API pages it needs).")

status_box = st.empty()
bar = st.progress(done_known / total_known if total_known else 0.0)

a1, a2, _ = st.columns([1, 1, 5])
verify = a1.button("Verify league IDs", disabled=not sel)
start = a2.button("▶ Start / Resume", type="primary", disabled=not sel)

if (verify or start) and not api_key:
    st.error("Enter your API key in the sidebar first.")
    st.stop()

# ---- verify league ids against the API
if verify:
    client = SM(api_key, on_wait=lambda s, why: status_box.warning(f"Waiting {s}s – {why}"))
    out = []
    for i, lid in enumerate(sel):
        status_box.info(f"Checking {label(lid)} ({i + 1}/{len(sel)})")
        try:
            m = ensure_meta(client, lid, max_age=0)
            ok = country_ok(LEAGUE_BY_ID[lid][0], m["api_country"])
            out.append({"id": lid, "expected": league_country(lid), "API league": m["api_name"],
                        "API country": m["api_country"], "country match": "✅" if ok else "⚠️ CHECK"})
        except ApiError as e:
            out.append({"id": lid, "expected": league_country(lid), "API league": "-",
                        "API country": str(e), "country match": "❌"})
    status_box.empty()
    st.dataframe(pd.DataFrame(out), use_container_width=True, hide_index=True)

# ---- main run
if start:
    client = SM(api_key, on_wait=lambda s, why: status_box.warning(f"Waiting {s}s – {why}"))
    state = {"xg": True, "xg_warning": None}
    problems, warnings = [], []

    # phase 1: season lists (cached in storage)
    live_metas = {}
    for i, lid in enumerate(sel):
        status_box.info(f"Reading season list {i + 1}/{len(sel)}: {label(lid)}")
        try:
            m = ensure_meta(client, lid)
            live_metas[lid] = m
            if not country_ok(LEAGUE_BY_ID[lid][0], m["api_country"]):
                warnings.append(f"{label(lid)}: API says this id is {m['api_name']} / {m['api_country']} – please check.")
        except ApiError as e:
            problems.append(f"{label(lid)}: {e}")
            if e.status in (401, 403):
                break

    plan = []
    for lid in sel:
        if lid in live_metas:
            for idx, s in enumerate(pick_seasons(live_metas[lid]["seasons"], back)):
                plan.append((lid, s, idx == 0))
    total = len(plan)
    done = sum(1 for lid, s, first in plan if (lid, s["id"]) in done_ids and not (refresh_latest and first))
    run_matches = 0
    show_counters(done, total, client.calls, 0, matches_sel)
    bar.progress(done / total if total else 0.0)

    for lid, s, first in plan:
        if (lid, s["id"]) in done_ids and not (refresh_latest and first):
            continue
        status_box.info(f"Requesting {label(lid)} · season {s['name']}  ({done + 1}/{total})")
        try:
            fixtures, note = fetch_season(client, lid, s, state)
            n = save_job(lid, s, fixtures, note, {})
            was_done = (lid, s["id"]) in done_ids
            done_ids.add((lid, s["id"]))
            if not was_done:
                done += 1
            run_matches += n
            if n == 0:
                warnings.append(f"{label(lid)} {s['name']}: 0 matches returned. {note or ''}")
        except ApiError as e:
            problems.append(f"{label(lid)} {s['name']}: {e}")
            if e.status in (401, 403):
                break
        con = db()
        matches_now = con.execute(f"SELECT COUNT(*) FROM fixtures WHERE league_id IN ({','.join('?' * len(sel))})",
                                  sel).fetchone()[0]
        con.close()
        show_counters(done, total, client.calls, run_matches, matches_now)
        bar.progress(done / total if total else 1.0)

    status_box.success(f"Run finished: {done} / {total} requests complete, {run_matches:,} matches saved this run.")
    if state["xg_warning"]:
        st.warning(state["xg_warning"])
    for w in warnings:
        st.warning(w)
    if problems:
        st.error("Some requests failed (press Start / Resume again to retry only those):\n\n" + "\n".join(f"- {p}" for p in problems))

# ---- progress per league
st.subheader("Progress")
con = db()
rows = []
for lid in (sel or []):
    seasons = pick_seasons(metas[lid], back) if lid in metas else []
    ids = {s["id"] for s in seasons}
    done_n = sum(1 for sid in ids if (lid, sid) in done_ids)
    m = con.execute("SELECT COUNT(*) FROM fixtures WHERE league_id=?", (lid,)).fetchone()[0]
    rows.append({"League": label(lid), "Seasons saved": f"{done_n} / {len(seasons) if seasons else '?'}",
                 "Matches": m})
con.close()
if rows:
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
else:
    st.caption("Select leagues to see progress.")

# ---- downloads
st.subheader("Download")
con = db()
have = [r[0] for r in con.execute("SELECT DISTINCT league_id FROM fixtures ORDER BY league_id") if r[0] in LEAGUE_BY_ID]
con.close()
if not have:
    st.caption("Nothing saved yet.")
else:
    d1, d2 = st.columns(2)
    with d1:
        pick = st.selectbox("Single league CSV", have, format_func=label)
        st.download_button("⬇ Download league CSV", df_to_csv(build_df(pick)),
                           file_name=f"{safe(league_country(pick))}_{pick}.csv", mime="text/csv")
    with d2:
        st.write("All completed requests (one CSV per league, zipped) – works even if a run was interrupted.")
        if st.button("Prepare ZIP of everything saved"):
            st.session_state["zip"] = build_zip()
        if st.session_state.get("zip"):
            data, n = st.session_state["zip"]
            st.download_button(f"⬇ Download ZIP ({n} leagues)", data, file_name="sportmonks_all_leagues.zip",
                               mime="application/zip")

    with st.expander("Check how Sportmonks stat names were mapped (use if a column comes out empty)"):
        con = db()
        seen = pd.read_sql_query("SELECT source, name, mapped AS mapped_to FROM seen_types ORDER BY mapped_to DESC, name", con)
        con.close()
        st.dataframe(seen, use_container_width=True, hide_index=True)

# ---- partial clear
st.subheader("Storage management")
with st.expander("Partial clear (free space)"):
    con = db()
    in_db = [r[0] for r in con.execute("SELECT DISTINCT league_id FROM jobs ORDER BY league_id") if r[0] in LEAGUE_BY_ID]
    con.close()
    if not in_db:
        st.caption("Storage is empty.")
    else:
        clr_leagues = st.multiselect("Leagues to clear", in_db, format_func=label, key="clr_l")
        season_opts = []
        if clr_leagues:
            con = db()
            season_opts = [r[0] for r in con.execute(
                f"SELECT DISTINCT season_name FROM jobs WHERE league_id IN ({','.join('?' * len(clr_leagues))}) "
                "ORDER BY season_name DESC", clr_leagues)]
            con.close()
        clr_seasons = st.multiselect("Only these seasons (leave empty = all seasons of those leagues)", season_opts)
        if st.button("Clear selected", disabled=not clr_leagues):
            con = db()
            q_l = ",".join("?" * len(clr_leagues))
            args = list(clr_leagues)
            extra = ""
            if clr_seasons:
                extra = f" AND season_name IN ({','.join('?' * len(clr_seasons))})"
                args += clr_seasons
            with con:
                con.execute(f"DELETE FROM fixtures WHERE league_id IN ({q_l}){extra}", args)
                con.execute(f"DELETE FROM jobs WHERE league_id IN ({q_l}){extra}", args)
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.execute("VACUUM")
            con.close()
            st.success("Cleared. Space released.")
            st.rerun()
    st.divider()
    sure = st.checkbox("I understand this deletes ALL saved data")
    if st.button("Clear everything", disabled=not sure):
        con = db()
        with con:
            for t in ("fixtures", "jobs", "league_meta", "seen_types"):
                con.execute(f"DELETE FROM {t}")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("VACUUM")
        con.close()
        st.session_state.pop("zip", None)
        st.rerun()
