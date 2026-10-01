# Sportmonks match data collector

Streamlit app that pulls fixtures, team stats and xG from Sportmonks API v3 and exports **one CSV per league**
(played + unplayed matches, sorted by date, home columns left / away columns right).

## Run on GitHub + Streamlit Community Cloud
1. Push `app.py`, `requirements.txt`, `.gitignore` to a GitHub repo.
2. At https://share.streamlit.io create an app pointing at `app.py`.
3. (Optional) In app Settings > Secrets add `SPORTMONKS_API_KEY = "your-key"`; otherwise type it in the sidebar.

Local: `pip install -r requirements.txt && streamlit run app.py`

## Persistence
Progress is stored in SQLite (`data/sportmonks_cache.sqlite`, override with env var `SM_DB_PATH`).
Interrupted? Press **Start / Resume** – finished league-seasons are skipped.
Community Cloud wipes local files when the app reboots or is redeployed, so use
**Sidebar > Backup / restore** (or the ZIP download) to keep your data safe.

## Things to verify on first run
- Use **Verify league IDs** – Eerste Divisie (79) and HNL (244) had no ID in your list; they are assumptions.
- xG needs the Sportmonks xG add-on. Without it the xG columns stay empty (the app warns you).
- Open "Check how Sportmonks stat names were mapped" to confirm every metric matched a Sportmonks stat type.
- Sportmonks limits ~3000 calls/hour per entity; the app waits automatically when it is hit.
