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
# Country IDs confirmed in the Sportmonks docs (core/countries examples). Others are read from the API and shown for checking.
KNOWN_COUNTRY_IDS = {"Poland": 2, "Brazil": 5, "Germany": 11, "France": 17, "Portugal": 20}
LEAGUE_ALIASES = {"Eerste Divisie": ["Eerste Divisie", "Keuken Kampioen Divisie"],
                  "HNL": ["HNL", "1. HNL", "SuperSport HNL"]}

def lkey(l): return str(l[0]) if l[0] else "name:" + l[1]
def llabel(l): return f"{l[2]} - {l[1]}" + (f" ({l[0]})" if l[0] else " (id by search)")
LBL = {lkey(l): llabel(l) for l in LEAGUES}
LEAGUE_BY_KEY = {lkey(l): l for l in LEAGUES}

METRICS = ["goals", "goalkeeper_saves", "shots_on_target", "big_chances_created", "corner_kicks",
           "total_crosses", "accurate_crosses", "successful_crosses_percentage", "accurate_long_passes",
           "successful_long_passes_percentage", "dribbles_percentage", "ground_duels_percentage",
           "aerial_duels_percentage", "tackles", "tackles_won", "xg", "xgot", "xg_set_play", "xg_open_play"]
norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
# Type IDs from docs.sportmonks.com/v3/definitions/types (fixture statistics + expected)
STAT_TYPES = {52: "goals", 57: "goalkeeper_saves", 86: "shots_on_target", 580: "big_chances_created",
              34: "corner_kicks", 98: "total_crosses", 99: "accurate_crosses",  # successful_crosses_percentage is calculated, not requested
              27264: "accurate_long_passes",  # SUCCESSFUL_LONG_PASSES
              27265: "successful_long_passes_percentage", 1605: "dribbles_percentage",
              78: "tackles", 27267: "tackles_won"}
# only these statistic types are requested (successful_crosses_percentage is NOT requested - it is calculated)
WANTED_IDS = ",".join(str(i) for i in STAT_TYPES)
EXPECTED_TYPES = {5304: "xg", 5305: "xgot", 7944: "xg_set_play", 7945: "xg_open_play"}
# ground_duels_percentage / aerial_duels_percentage: no team-level type in the fixture statistics docs -> left blank

# Fallback when a type_id differs from the docs: match on the developer_name Sportmonks sends
NAME_MAP = {norm(m): m for m in METRICS}
NAME_MAP.update({"corners": "corner_kicks", "saves": "goalkeeper_saves", "successfullongpasses": "accurate_long_passes",
                 "successfuldribblespercentage": "dribbles_percentage", "tackleswon": "tackles_won",
                 "groundduelswonpercentage": "ground_duels_percentage", "aerialduelswonpercentage": "aerial_duels_percentage",
                 "expectedgoals": "xg", "expectedgoalsontarget": "xgot", "expectedgoalssetplay": "xg_set_play",
                 "expectedgoalsopenplay": "xg_open_play"})

NAME_MAP.pop("successfulcrossespercentage", None)  # calculated from total/accurate crosses

def map_stat(tid, dev):
    return STAT_TYPES.get(int(tid)) or EXPECTED_TYPES.get(int(tid)) or NAME_MAP.get(norm(dev))

def verdict(l, m):
    """❌ = wrong league (ID differs, or country_id differs from a doc-confirmed one) -> skipped.
    ⚠️ = ID fine but names differ from your list (e.g. API says 'United Kingdom' for England) -> fetched anyway.
    ✅ = everything agrees."""
    cid_known = KNOWN_COUNTRY_IDS.get(l[2])
    if (l[0] and m["id"] != l[0]) or (cid_known is not None and m.get("country_id") not in (None, cid_known)):
        return "❌"
    a, b = norm(l[1]), norm(m["name"])
    name_ok = a == b or a in b or b in a or any(norm(x) in b or b in norm(x) for x in LEAGUE_ALIASES.get(l[1], []))
    ca, cb = norm(l[2]), norm(m["country"])
    country_ok = ca == cb or ca in cb or cb in ca or (cid_known is not None and m.get("country_id") == cid_known)
    return "✅" if name_ok and country_ok else "⚠️"

# ---------------- storage ----------------
def db():
    c = sqlite3.connect(DB)
    c.executescript("""CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, j TEXT);
    CREATE TABLE IF NOT EXISTS prog(k TEXT, sid INTEGER, sname TEXT, next_page INTEGER, total INTEGER, done INTEGER,
        PRIMARY KEY(k,sid));
    CREATE TABLE IF NOT EXISTS reqlog(ts REAL);
    CREATE TABLE IF NOT EXISTS fx(k TEXT, fid INTEGER, j TEXT, PRIMARY KEY(k,fid));""")
    try: c.execute("ALTER TABLE prog ADD COLUMN tm INTEGER DEFAULT 0")  # total matches in the season
    except sqlite3.OperationalError: pass
    return c

def get_meta(k):
    r = db().execute("SELECT j FROM meta WHERE k=?", (k,)).fetchone()
    return json.loads(r[0]) if r else None

# ---------------- API ----------------
class ApiError(Exception): pass

CFG = {"limit": 3000, "rl": None}

def usage():
    """API calls made in the last hour (persisted), and seconds until the oldest one drops out of the window."""
    now = time.time()
    n, oldest = db().execute("SELECT COUNT(*), MIN(ts) FROM reqlog WHERE ts>?", (now - 3600,)).fetchone()
    return n, (int(oldest + 3600 - now) if oldest else 0)

def api(key, path, params=None):
    for attempt in range(4):
        used, wait = usage()
        if used >= CFG["limit"]:
            raise ApiError(f"Hourly request limit reached ({used}/{CFG['limit']}). Try again in about {wait // 60 + 1} min.")
        c = db(); c.execute("INSERT INTO reqlog VALUES(?)", (time.time(),)); c.commit()
        r = requests.get(BASE + path, params=params or {}, headers={"Authorization": key}, timeout=60)
        if r.status_code == 429:
            time.sleep(min(int(r.headers.get("Retry-After", 60)), 120)); continue
        if r.status_code != 200:
            raise ApiError(f"HTTP {r.status_code}: {r.text[:200]}")
        j = r.json(); CFG["rl"] = j.get("rate_limit")
        if "data" not in j:
            msg = str(j.get("message") or j)[:300]
            if "no result" in msg.lower():  # nothing matched: treat as an empty page
                return {"data": [], "pagination": {"has_more": False}}
            raise ApiError(f"{path} returned no data: {msg}")
        return j
    raise ApiError("Rate limit persisted; try again later (progress is saved).")

def fixture_ids(node, ids=None):
    """Collect fixture ids from a /schedules/seasons/{id} response (stages -> rounds -> fixtures)."""
    ids = set() if ids is None else ids
    if isinstance(node, dict):
        for kk, v in node.items():
            if kk == "fixtures" and isinstance(v, list):
                ids.update(f["id"] for f in v if isinstance(f, dict) and "id" in f)
            else: fixture_ids(v, ids)
    elif isinstance(node, list):
        for x in node: fixture_ids(x, ids)
    return ids

def fetch_meta(key, l):
    if l[0]:
        d = api(key, f"/leagues/{l[0]}", {"include": "country;seasons"}).get("data")
    else:  # no league id given: find the country id, list that country's leagues, pick by name
        cs = api(key, f"/core/countries/search/{l[2]}").get("data") or []
        cs = [cs] if isinstance(cs, dict) else cs
        cid = next((x["id"] for x in cs if norm(x.get("name")) == norm(l[2])), None)
        if cid is None: raise ApiError(f"Country '{l[2]}' not found in Sportmonks")
        found, page = [], 1
        while True:
            j = api(key, f"/leagues/countries/{cid}", {"include": "country;seasons", "per_page": 50, "page": page})
            found += j.get("data") or []
            if not (j.get("pagination") or {}).get("has_more"): break
            page += 1
        names = LEAGUE_ALIASES.get(l[1], [l[1]])
        hit = [x for x in found if any(norm(x["name"]) == norm(a_) for a_ in names)] or \
              [x for x in found if any(norm(a_) in norm(x["name"]) for a_ in names)]
        if not hit:
            raise ApiError(f"No '{l[1]}' among {l[2]} (country id {cid}) leagues. Available: " +
                           ", ".join(f"{x['id']} {x['name']}" for x in found))
        d = hit[0]
    if not d or isinstance(d, list):
        raise ApiError(f"League {l[0] or l[1]} was not returned by Sportmonks (not in your plan, or wrong id)")
    seasons = sorted(d.get("seasons") or [], key=lambda s: s.get("starting_at") or "", reverse=True)
    m = {"id": d["id"], "name": d["name"], "country": (d.get("country") or {}).get("name", ""),
         "country_id": d.get("country_id") or (d.get("country") or {}).get("id"),
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
    out["raw"] = {"home": {}, "away": {}}
    exp = fx.get("expected") or fx.get("xGFixture") or fx.get("xgfixture") or []
    for st_ in (fx.get("statistics") or []) + exp:
        sd = side.get(st_.get("participant_id"))
        if sd and st_.get("type_id") is not None:
            out["raw"][sd][str(st_["type_id"])] = [(st_.get("type") or {}).get("developer_name"),
                                                   (st_.get("data") or {}).get("value", st_.get("value"))]
    return out

# ---------------- export ----------------
def r0(v):
    """Round half up to a whole number (Python's round() would round 0.5 down to 0 and 2.5 down to 2)."""
    return int(math.floor(v + 0.5)) if isinstance(v, (int, float)) and not isinstance(v, bool) else v

def r2(v):
    return round(v, 2) if isinstance(v, float) else v

def build_df(k):
    c = db(); m = get_meta(k)
    rows = [json.loads(r[0]) for r in c.execute("SELECT j FROM fx WHERE k=?", (k,))]
    if not rows or not m: return pd.DataFrame()
    cols = METRICS[:METRICS.index("tackles_won") + 1] + ["tackles_won_percentage"] + METRICS[METRICS.index("tackles_won") + 1:]
    out = []
    for r in rows:
        h, a = dict(r["home"]), dict(r["away"])
        for sd_, dst in (("home", h), ("away", a)):
            for tid, (dev, v) in (r.get("raw") or {}).get(sd_, {}).items():
                mm = map_stat(tid, dev)
                if mm == "goals": dst.setdefault(mm, pick(v, False))  # scores take priority
                elif mm: dst[mm] = pick(v, "percentage" in mm)
        for s in (h, a):
            tc, ac = s.get("total_crosses"), s.get("accurate_crosses")
            s["successful_crosses_percentage"] = r0(ac / tc * 100) if tc and ac is not None else None
            t, w = s.get("tackles"), s.get("tackles_won")
            s["tackles_won_percentage"] = r0(w / t * 100) if t and w is not None else None
        row = {"league_country": f"{m['country']} {m['name']}", "season": r["season"], "date": r["date"],
               "home_team": r["name"].get("home", ""), "away_team": r["name"].get("away", "")}
        for c_ in cols:
            f = r0 if "percentage" in c_ else r2
            row[f"home_{c_}"], row[f"away_{c_}"] = f(h.get(c_)), f(a.get(c_))
        out.append(row)
    # dtype=object keeps every value exactly as stored (pandas would otherwise turn int columns with gaps into floats)
    return pd.DataFrame(out, dtype=object).sort_values("date", kind="stable").reset_index(drop=True)

def csv_name(k):
    m = get_meta(k) or {}
    return re.sub(r"[^A-Za-z0-9]+", "_", f"{m.get('country','')}_{m.get('name',k)}").strip("_") + ".csv"

# ---------------- UI ----------------
st.set_page_config(page_title="Sportmonks Collector", layout="wide")
st.title("Sportmonks league stats collector")

with st.sidebar:
    key = st.text_input("Sportmonks API key", type="password", value=st.secrets.get("SPORTMONKS_KEY", "") if hasattr(st, "secrets") else "")
    n_seasons = st.slider("Seasons (1 = current only, 8 = current + 7 previous)", 1, 8, 1)
    CFG["limit"] = st.number_input("Request limit per hour (your Sportmonks plan)", 1, 100000, 3000, step=100)
    use_filter = st.checkbox("Request only the needed stat types (filter)", value=True,
                             help="On: Sportmonks is asked for the listed stat types only (successful_crosses_percentage excluded). "
                                  "Off: all stats are requested. Use the test in 'Stat types seen' to see which one returns your stats.")
    size = os.path.getsize(DB) / 1e6 if os.path.exists(DB) else 0
    st.caption(f"Persistent storage used: {size:.1f} MB")

all_keys = list(LBL)
st.session_state.setdefault("sel", [])
b1, b2, _ = st.columns([1, 1, 6])
if b1.button("Select all"): st.session_state["sel"] = all_keys
if b2.button("Clear selection"): st.session_state["sel"] = []
sel = st.multiselect("Leagues", all_keys, key="sel", format_func=lambda k: LBL[k])

def status():
    """(requests made, requests planned). Planned per league: 1 league call + per season 1 schedule call + ceil(matches/50) pages."""
    c = db(); made = planned = 0
    for k in sel:
        planned += 1; made += 1 if get_meta(k) else 0
        m = get_meta(k)
        rows = {r[0]: r[1:] for r in c.execute("SELECT sid, next_page-1, total FROM prog WHERE k=?", (k,))}
        for x in (wanted(m, n_seasons) if m else []):
            if x["id"] in rows: made += 1 + rows[x["id"]][0]; planned += 1 + max(rows[x["id"]][1], 1)
            else: planned += 2
    return made, planned

def match_status():
    """(matches fetched, total matches known, seasons whose total is not known yet)."""
    c = db(); got = tot = unknown = 0
    for k in sel:
        m = get_meta(k)
        if not m: unknown += 1; continue
        rows = {r[0]: r[1:] for r in c.execute("SELECT sid, sname, tm FROM prog WHERE k=?", (k,))}
        for x in wanted(m, n_seasons):
            if x["id"] not in rows: unknown += 1; continue
            sname, tm = rows[x["id"]]
            g = c.execute("SELECT COUNT(*) FROM fx WHERE k=? AND json_extract(j,'$.season')=?", (k, sname)).fetchone()[0]
            got += g; tot += max(tm or 0, g)
    return got, tot, unknown

def match_text():
    g, t, u = match_status()
    return f"Matches fetched: {g} / {t}" + (f" (+ {u} season(s) not counted yet)" if u else "")

made, planned = status()
st.progress(min(made / planned, 1.0) if planned else 0.0, text=f"Requests made: {made} / {planned}")
_u, _w = usage()
st.caption(match_text() + f" | API calls in the last hour: {_u}/{CFG['limit']}" + (f" - oldest drops out in {_w // 60 + 1} min" if _u else ""))

if st.button("Start / resume", type="primary", disabled=not (key and sel)):
    bar, msg = st.progress(0.0), st.empty()
    try:
        for k in sel:
            try:
                l = LEAGUE_BY_KEY[k]
                m = get_meta(k)
                if not m or "country_id" not in m: m = fetch_meta(key, l)
                v = verdict(l, m)
                if v == "❌":
                    st.error(f"{LBL[k]}: API returned {m['country']} {m['name']} (id {m['id']}, country_id {m.get('country_id')}). Skipping.")
                    continue
                if v == "⚠️":
                    st.warning(f"{LBL[k]}: API calls it {m['country']} {m['name']} (id {m['id']}, country_id {m.get('country_id')}). "
                               "League ID matches, so it is fetched anyway.")
                seasons = wanted(m, n_seasons)
                for s in seasons:
                    c = db()
                    r_ = c.execute("SELECT tm, done FROM prog WHERE k=? AND sid=?", (k, s["id"])).fetchone()
                    if r_ is None or (not r_[0] and not r_[1]):  # count the season's matches (played + unplayed) first
                        try: tm = len(fixture_ids(api(key, f"/schedules/seasons/{s['id']}").get("data")))
                        except ApiError as e:
                            if str(e).startswith("Hourly"): raise
                            tm = 0
                        c.execute("INSERT OR IGNORE INTO prog(k,sid,sname,next_page,total,done,tm) VALUES(?,?,?,1,1,0,0)", (k, s["id"], s["name"]))
                        c.execute("UPDATE prog SET tm=?, total=? WHERE k=? AND sid=?", (tm, max(1, math.ceil(tm / 50)), k, s["id"])); c.commit()
                    while True:
                        nxt, done, tm = c.execute("SELECT next_page, done, tm FROM prog WHERE k=? AND sid=?", (k, s["id"])).fetchone()
                        if done: break
                        d = api(key, "/fixtures", {"include": "participants;scores;statistics.type;xGFixture",
                                                   "filters": f"fixtureSeasons:{s['id']}" + (f";fixtureStatisticTypes:{WANTED_IDS}" if use_filter else ""), "per_page": 50, "page": nxt})
                        pg = d.get("pagination") or {}
                        total = math.ceil(tm / 50) if tm else (nxt + 1 if pg.get("has_more") else nxt)  # pagination.count is per-page, not a total
                        for f in d.get("data", []):
                            p = parse(f); p["season"] = s["name"]
                            c.execute("REPLACE INTO fx VALUES(?,?,?)", (k, f["id"], json.dumps(p)))
                        c.execute("UPDATE prog SET next_page=?, total=?, done=? WHERE k=? AND sid=?",
                                  (nxt + 1, max(total, 1), 0 if pg.get("has_more") else 1, k, s["id"]))
                        c.commit()
                        mm, pp = status()
                        bar.progress(min(mm / pp, 1.0) if pp else 1.0)
                        msg.info(f"{LBL[k]} | season {s['name']} | page {nxt}/{total} | {match_text()} | requests {mm}/{pp} | API calls this hour {usage()[0]}/{CFG['limit']}")
            except ApiError as e:
                if str(e).startswith("Hourly"): raise
                st.error(f"{LBL[k]} skipped: {e}")

        msg.success("Run finished (see any errors above for skipped leagues).")
    except Exception as e:
        st.error(f"Interrupted: {type(e).__name__}: {e}. Progress is saved - press Start / resume to continue, or download what's done below.")

# ---- verification ----
with st.expander("League & country ID verification"):
    if st.button("Fetch league + country IDs for selected leagues", disabled=not (key and sel)):
        try:
            for k in sel:
                if not get_meta(k) or "country_id" not in get_meta(k): fetch_meta(key, LEAGUE_BY_KEY[k])
        except Exception as e:
            st.error(f"{type(e).__name__}: {e}")
    rows, by_country = [], {}
    for k in sel:
        m = get_meta(k)
        if m: by_country.setdefault(LEAGUE_BY_KEY[k][2], set()).add(m.get("country_id"))
    for k in sel:
        m, l = get_meta(k), LEAGUE_BY_KEY[k]
        v = verdict(l, m) if m else "-"
        if m and len(by_country.get(l[2], ())) > 1: v = "❌"  # leagues of one country must share one country_id
        rows.append({"your list": LBL[k], "API league": f"{m['name']} ({m['id']})" if m else "not fetched yet",
                     "API country": m["country"] if m else "-", "API country_id": (m.get("country_id") or "-") if m else "-",
                     "doc-confirmed country_id": KNOWN_COUNTRY_IDS.get(l[2], "-"), "result": v})
    st.dataframe(pd.DataFrame(rows), use_container_width=True) if rows else st.write("Select leagues first.")
    st.caption("✅ agrees | ⚠️ ID matches but names differ (still fetched) | ❌ wrong league (skipped)")

# ---- filter test ----
with st.expander("Test: stat filter vs no filter on one fixture (2 requests)"):
    if st.button("Run test on a stored, played fixture", disabled=not key):
        fid = None
        for f_, j_ in db().execute("SELECT fid, j FROM fx LIMIT 500"):
            if json.loads(j_)["home"].get("goals") is not None: fid = f_; break
        if fid is None: st.warning("Fetch at least one league first so there is a played fixture to test.")
        else:
            try:
                for label, flt in (("WITH filter", f"fixtureStatisticTypes:{WANTED_IDS}"), ("WITHOUT filter", None)):
                    p = {"include": "participants;statistics.type;xGFixture"}
                    if flt: p["filters"] = flt
                    d_ = api(key, f"/fixtures/{fid}", p)["data"]
                    exp = d_.get("expected") or d_.get("xGFixture") or d_.get("xgfixture") or []
                    rows_ = [{"source": "statistics", "type_id": x.get("type_id"), "name": (x.get("type") or {}).get("developer_name"),
                              "team": x.get("participant_id"), "value": (x.get("data") or {}).get("value")} for x in d_.get("statistics", [])]
                    rows_ += [{"source": "xG", "type_id": x.get("type_id"), "name": "-", "team": x.get("participant_id"),
                               "value": (x.get("data") or {}).get("value", x.get("value"))} for x in exp]
                    st.write(f"**{label}** - fixture {fid}: {len(d_.get('statistics', []))} stats, {len(exp)} xG items; response keys: {sorted(d_)}")
                    st.dataframe(pd.DataFrame(rows_), use_container_width=True)
            except Exception as e:
                st.error(f"{type(e).__name__}: {e}")

# ---- diagnostics ----
with st.expander("Stat types seen in stored data (find unmapped / missing stats)"):
    seen = {}
    for (j,) in db().execute("SELECT j FROM fx LIMIT 3000"):
        for sd_ in json.loads(j).get("raw", {}).values():
            for tid, (dev, _) in sd_.items():
                seen[(int(tid), dev)] = seen.get((int(tid), dev), 0) + 1
    if seen:
        st.dataframe(pd.DataFrame([{"type_id": t, "developer_name": d, "seen": n, "mapped_to": map_stat(t, d) or "-"}
                                   for (t, d), n in sorted(seen.items())]), use_container_width=True)
    else:
        st.write("No raw stats stored yet (data fetched before this version has none - clear and re-fetch).")

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
