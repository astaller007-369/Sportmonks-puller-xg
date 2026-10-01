import io, json, math, os, re, sqlite3, time, zipfile
import pandas as pd
import requests
import streamlit as st

BASE = "https://api.sportmonks.com/v3/football"
DATA_DIR = os.environ.get("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB = os.path.join(DATA_DIR, "store.db")

# (id, name, country). id None -> resolved by name search + country match
LEAGUES = [
    (543, "Ekstraklasa", "Poland"), (648, "Serie A", "Brazil"), (82, "Bundesliga", "Germany"),
    (85, "2. Bundesliga", "Germany"), (301, "Ligue 1", "France"), (304, "Ligue 2", "France"),
    (462, "Liga Portugal", "Portugal"), (564, "La Liga", "Spain"), (567, "La Liga 2", "Spain"),
    (72, "Eredivisie", "Netherlands"), (636, "Liga Profesional de Futbol", "Argentina"),
    (573, "Allsvenskan", "Sweden"), (325, "Super League", "Greece"), (591, "Super League", "Switzerland"),
    (1356, "A-League Men", "Australia"), (181, "Admiral Bundesliga", "Austria"),
    (486, "Premier League", "Russia"), (384, "Serie A", "Italy"), (387, "Serie B", "Italy"),
    (271, "Superliga", "Denmark"), (672, "Liga BetPlay", "Colombia"), (600, "Super Lig", "Turkey"),
    (360, "Premier Division", "Republic of Ireland"), (8, "Premier League", "England"),
    (9, "Championship", "England"), (968, "J1 League", "Japan"), (208, "Pro League", "Belgium"),
    (211, "Challenger Pro League", "Belgium"), (501, "Premiership", "Scotland"),
    (444, "Eliteserien", "Norway"), (1007, "Indian Super League", "India"),
    (830, "Premier League", "Egypt"), (806, "Premier League", "South Africa"),
    (1064, "Thai Premier League", "Thailand"), (743, "Liga MX", "Mexico"),
    (779, "Major League Soccer", "United States"), (944, "Pro League", "Saudi Arabia"),
    (651, "Serie B", "Brazil"), (1034, "K League 1", "South Korea"), (12, "League One", "England"),
    (14, "League Two", "England"), (None, "Eerste Divisie", "Netherlands"), (None, "HNL", "Croatia"),
]
def lkey(l): return str(l[0]) if l[0] else "name:" + l[1]
def llabel(l): return f"{l[2]} - {l[1]}" + (f" ({l[0]})" if l[0] else " (id by search)")
LBL = {lkey(l): llabel(l) for l in LEAGUES}
LEAGUE_BY_KEY = {lkey(l): l for l in LEAGUES}

METRICS = ["goals", "goalkeeper_saves", "shots_on_target", "big_chances_created", "corner_kicks",
           "accurate_crosses", "successful_crosses_percentage", "accurate_long_passes",
           "successful_long_passes_percentage", "dribbles_percentage", "ground_duels_percentage",
           "aerial_duels_percentage", "tackles", "tackles_won", "xg", "xgot", "xg_set_play", "xg_open_play"]
norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
# Type IDs from docs.sportmonks.com/v3/definitions/types (fixture statistics + expected)
STAT_TYPES = {52: "goals", 57: "goalkeeper_saves", 86: "shots_on_target", 580: "big_chances_created",
              34: "corner_kicks", 99: "accurate_crosses", 1533: "successful_crosses_percentage",
              27264: "accurate_long_passes",  # SUCCESSFUL_LONG_PASSES
              27265: "successful_long_passes_percentage", 1605: "dribbles_percentage",
              78: "tackles", 27267: "tackles_won"}
EXPECTED_TYPES = {5304: "xg", 5305: "xgot", 7944: "xg_set_play", 7945: "xg_open_play"}
# ground_duels_percentage / aerial_duels_percentage: no team-level type in the fixture statistics docs -> left blank

# ---------------- storage ----------------
def db():
    c = sqlite3.connect(DB)
    c.executescript("""CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, j TEXT);
    CREATE TABLE IF NOT EXISTS prog(k TEXT, sid INTEGER, sname TEXT, next_page INTEGER, total INTEGER, done INTEGER,
        PRIMARY KEY(k,sid));
    CREATE TABLE IF NOT EXISTS fx(k TEXT, fid INTEGER, j TEXT, PRIMARY KEY(k,fid));""")
    return c

def get_meta(k):
    r = db().execute("SELECT j FROM meta WHERE k=?", (k,)).fetchone()
    return json.loads(r[0]) if r else None

# ---------------- API ----------------
class ApiError(Exception): pass

def api(key, path, params=None):
    for attempt in range(4):
        r = requests.get(BASE + path, params=params or {}, headers={"Authorization": key}, timeout=60)
        if r.status_code == 429:
            time.sleep(min(int(r.headers.get("Retry-After", 60)), 120)); continue
        if r.status_code != 200:
            raise ApiError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()
    raise ApiError("Rate limit persisted; try again later (progress is saved).")

def fetch_meta(key, l):
    if l[0]:
        d = api(key, f"/leagues/{l[0]}", {"include": "country;seasons"})["data"]
    else:
        res = api(key, f"/leagues/search/{l[1]}", {"include": "country;seasons"})["data"]
        res = [x for x in res if norm((x.get("country") or {}).get("name")) == norm(l[2])]
        if not res: raise ApiError(f"No league '{l[1]}' found for {l[2]}")
        d = res[0]
    seasons = sorted(d.get("seasons") or [], key=lambda s: s.get("starting_at") or "", reverse=True)
    m = {"id": d["id"], "name": d["name"], "country": (d.get("country") or {}).get("name", ""),
         "seasons": [{"id": s["id"], "name": s["name"], "current": s.get("is_current")} for s in seasons]}
    c = db(); c.execute("REPLACE INTO meta VALUES(?,?)", (lkey(l), json.dumps(m))); c.commit()
    return m

def wanted(m, n):
    """Any upcoming season(s) + current season (is_current flag) + the n-1 seasons before it."""
    ss = m["seasons"]  # newest first
    ci = next((i for i, x in enumerate(ss) if x.get("current")), 0)
    return ss[:ci + n]  # ss[:ci] = upcoming seasons, if Sportmonks lists any

def pick(v, pct):
    if isinstance(v, dict):
        for k in (("percentage",) if pct else ()) + ("accurate", "total", "count", "value", "percentage"):
            if isinstance(v.get(k), (int, float)): return v[k]
        return next((x for x in v.values() if isinstance(x, (int, float))), None)
    return v

def parse(fx):
    side = {p["id"]: p["meta"]["location"] for p in fx.get("participants", [])}
    out = {"date": fx.get("starting_at"), "name": {side[p["id"]]: p["name"] for p in fx.get("participants", [])},
           "home": {}, "away": {}}
    for s in fx.get("scores", []):
        if s.get("description") == "CURRENT":
            out[s["score"]["participant"]]["goals"] = s["score"]["goals"]
    exp = fx.get("expected") or fx.get("xGFixture") or fx.get("xgfixture") or []
    for st_, tmap in [(x, STAT_TYPES) for x in fx.get("statistics") or []] + [(x, EXPECTED_TYPES) for x in exp]:
        m, sd = tmap.get(st_.get("type_id")), side.get(st_.get("participant_id"))
        if m and sd:
            v = pick((st_.get("data") or {}).get("value", st_.get("value")), "percentage" in m)
            if m == "goals": out[sd].setdefault(m, v)  # scores take priority
            else: out[sd][m] = v
    return out

# ---------------- export ----------------
def build_df(k):
    c = db(); m = get_meta(k)
    rows = [json.loads(r[0]) for r in c.execute("SELECT j FROM fx WHERE k=?", (k,))]
    if not rows or not m: return pd.DataFrame()
    cols = METRICS[:METRICS.index("tackles_won") + 1] + ["tackles_won_percentage"] + METRICS[METRICS.index("tackles_won") + 1:]
    out = []
    for r in rows:
        h, a = r["home"], r["away"]
        for s in (h, a):
            t, w = s.get("tackles"), s.get("tackles_won")
            s["tackles_won_percentage"] = round(w / t * 100, 2) if t and w is not None else None
        row = {"league_country": f"{m['country']} {m['name']}", "season": r["season"], "date": r["date"],
               "home_team": r["name"].get("home", ""), "away_team": r["name"].get("away", "")}
        for c_ in cols:
            row[f"home_{c_}"], row[f"away_{c_}"] = h.get(c_), a.get(c_)
        out.append(row)
    return pd.DataFrame(out).sort_values("date", kind="stable").reset_index(drop=True)

def csv_name(k):
    m = get_meta(k) or {}
    return re.sub(r"[^A-Za-z0-9]+", "_", f"{m.get('country','')}_{m.get('name',k)}").strip("_") + ".csv"

# ---------------- UI ----------------
st.set_page_config(page_title="Sportmonks Collector", layout="wide")
st.title("Sportmonks league stats collector")

with st.sidebar:
    key = st.text_input("Sportmonks API key", type="password", value=st.secrets.get("SPORTMONKS_KEY", "") if hasattr(st, "secrets") else "")
    n_seasons = st.slider("Seasons (1 = current only, 8 = current + 7 previous)", 1, 8, 1)
    size = os.path.getsize(DB) / 1e6 if os.path.exists(DB) else 0
    st.caption(f"Persistent storage used: {size:.1f} MB")

all_keys = list(LBL)
st.session_state.setdefault("sel", [])
b1, b2, _ = st.columns([1, 1, 6])
if b1.button("Select all"): st.session_state["sel"] = all_keys
if b2.button("Clear selection"): st.session_state["sel"] = []
sel = st.multiselect("Leagues", all_keys, key="sel", format_func=lambda k: LBL[k])

def status():
    c = db(); made = planned = 0
    for k in sel:
        planned += 1; made += 1 if get_meta(k) else 0
        for done, tot in c.execute("SELECT next_page-1, total FROM prog WHERE k=?", (k,)):
            made += done; planned += max(tot, 1)
        m = get_meta(k)
        if m and not c.execute("SELECT 1 FROM prog WHERE k=?", (k,)).fetchone():
            planned += len(wanted(m, n_seasons))
    return made, planned

made, planned = status()
st.progress(min(made / planned, 1.0) if planned else 0.0, text=f"Requests made: {made} / {planned}")

if st.button("Start / resume", type="primary", disabled=not (key and sel)):
    bar, msg = st.progress(0.0), st.empty()
    try:
        for k in sel:
            l = LEAGUE_BY_KEY[k]
            m = get_meta(k) or fetch_meta(key, l)
            if l[0] and (m["id"] != l[0] or norm(m["country"]) != norm(l[2])):
                st.error(f"{LBL[k]}: API returned {m['country']} {m['name']} (id {m['id']}). Skipping.")
                continue
            seasons = wanted(m, n_seasons)
            for s in seasons:
                c = db()
                c.execute("INSERT OR IGNORE INTO prog VALUES(?,?,?,1,1,0)", (k, s["id"], s["name"])); c.commit()
                while True:
                    nxt, done = c.execute("SELECT next_page, done FROM prog WHERE k=? AND sid=?", (k, s["id"])).fetchone()
                    if done: break
                    d = api(key, "/fixtures", {"include": "participants;scores;statistics;xGFixture",
                                               "filters": f"fixtureSeasons:{s['id']}", "per_page": 50, "page": nxt})
                    pg = d.get("pagination") or {}
                    total = math.ceil(pg["count"] / pg.get("per_page", 50)) if pg.get("count") else (nxt + 1 if pg.get("has_more") else nxt)
                    for f in d.get("data", []):
                        p = parse(f); p["season"] = s["name"]
                        c.execute("REPLACE INTO fx VALUES(?,?,?)", (k, f["id"], json.dumps(p)))
                    c.execute("UPDATE prog SET next_page=?, total=?, done=? WHERE k=? AND sid=?",
                              (nxt + 1, max(total, 1), 0 if pg.get("has_more") else 1, k, s["id"]))
                    c.commit()
                    mm, pp = status()
                    bar.progress(min(mm / pp, 1.0) if pp else 1.0)
                    msg.info(f"{LBL[k]} | season {s['name']} | page {nxt}/{total} | requests {mm}/{pp}")
        msg.success("All requests completed.")
    except Exception as e:
        st.error(f"Interrupted: {e}. Progress is saved - press Start / resume to continue, or download what's done below.")

# ---- verification ----
with st.expander("League ID verification"):
    rows = []
    for k in sel:
        m, l = get_meta(k), LEAGUE_BY_KEY[k]
        ok = bool(m) and (not l[0] or (m["id"] == l[0] and norm(m["country"]) == norm(l[2])))
        rows.append({"expected": LBL[k], "API says": f"{m['country']} - {m['name']} ({m['id']})" if m else "not fetched yet",
                     "ok": "✅" if ok and m else ("❌" if m else "-")})
    st.dataframe(pd.DataFrame(rows), use_container_width=True) if rows else st.write("Select leagues first.")

# ---- downloads ----
st.subheader("Downloads (one CSV per league)")
c = db()
have = [r[0] for r in c.execute("SELECT DISTINCT k FROM fx")]
if not have: st.write("No data stored yet.")
else:
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        for k in have:
            df = build_df(k)
            if len(df): z.writestr(csv_name(k), df.to_csv(index=False))
    st.download_button("⬇️ Download all completed leagues (ZIP)", zbuf.getvalue(), "leagues.zip", "application/zip")
    for k in have:
        n = c.execute("SELECT COUNT(*) FROM fx WHERE k=?", (k,)).fetchone()[0]
        pr = c.execute("SELECT SUM(done), COUNT(*) FROM prog WHERE k=?", (k,)).fetchone()
        c1, c2 = st.columns([4, 1])
        c1.write(f"{LBL.get(k, k)} - {n} matches, seasons done {pr[0]}/{pr[1]}")
        c2.download_button("CSV", build_df(k).to_csv(index=False), csv_name(k), "text/csv", key="dl" + k)

# ---- clear ----
st.subheader("Free up storage")
clr = st.multiselect("Leagues to clear", have, format_func=lambda k: LBL.get(k, k))
c1, c2, _ = st.columns([1, 1, 4])
if c1.button("Clear selected leagues", disabled=not clr):
    for k in clr:
        for t in ("fx", "prog", "meta"): c.execute(f"DELETE FROM {t} WHERE k=?", (k,))
    c.commit(); c.execute("VACUUM"); st.rerun()
if c2.button("Clear everything", disabled=not have):
    for t in ("fx", "prog", "meta"): c.execute(f"DELETE FROM {t}")
    c.commit(); c.execute("VACUUM"); st.rerun()
