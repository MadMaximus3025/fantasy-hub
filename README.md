# Fantasy Hub

Dashboard for an ESPN fantasy basketball (9-cat) league: roster with injuries and news, weekly games-left slate vs your opponent, and a projected category matchup.

## Deploy (Render, free)

1. Push this folder to a GitHub repo.
2. On render.com: New > Blueprint > pick the repo. Render reads `render.yaml`.
3. Enter `LEAGUE_ID`, `ESPN_S2`, `SWID` (and `TEAM_ID` if needed) when asked.
4. Open the `https://fantasy-hub-xxxx.onrender.com` URL Render gives you.

Free plan sleeps after 15 min idle; first visit after that takes ~1 min to wake.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in LEAGUE_ID, YEAR, ESPN_S2, SWID
uvicorn server:app --reload
```

Open http://localhost:8000.

- `LEAGUE_ID`: the number after `leagueId=` in your ESPN league URL.
- `YEAR`: the season's end year (2026-27 season = `2027`).
- `ESPN_S2` / `SWID`: private leagues only. On espn.com, logged in, open DevTools > Application > Cookies.

Data is cached for 5 minutes; the Refresh button forces a reload.

## Test

```bash
python test_server.py
```
