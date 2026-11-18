import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.request import urlopen

from espn_api.basketball import League
from espn_api.basketball.constant import PRO_TEAM_MAP
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

COUNTING = ["PTS", "REB", "AST", "STL", "BLK", "3PM", "TO"]
CATS = ["FG%", "FT%", "3PM", "PTS", "REB", "AST", "STL", "BLK", "TO"]
CACHE_SECONDS = 300

app = FastAPI()
_cache = {"at": 0, "data": None}
_gamelogs = {}

GAMELOG_URL = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{}/gamelog?season={}"
# ESPN's public site API uses different team codes from the fantasy API
SITE_TO_FANTASY = {"GS": "GSW", "NO": "NOP", "NY": "NYK", "PHI": "PHL", "PHX": "PHO", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS"}
H2H_SEASONS = 4


def current_season():
    return int(os.environ.get("YEAR", date.today().year + 1))


def load_env(path=Path(__file__).with_name(".env")):
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def week_bounds(today):
    # shortcut: assumes Mon-Sun matchup weeks; ESPN stretches some weeks (All-Star, opening week).
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=6)


def game_dates(player, start, end):
    return [g["date"].date() for g in player.schedule.values() if start <= g["date"].date() <= end]


def plays(player):
    # IR-slot players can't score for you even when listed day-to-day
    return player.injuryStatus != "OUT" and player.lineupSlot != "IR"


def season_avg(player):
    return player.stats.get(f"{player.year}_total", {}).get("avg") or {}


def daily_slate(roster, monday):
    days = [monday + timedelta(days=i) for i in range(7)]
    counts = {d: 0 for d in days}
    for p in filter(plays, roster):
        for d in game_dates(p, days[0], days[-1]):
            counts[d] += 1
    return [counts[d] for d in days]


def scores(current_cats):
    return {k: (v or {}).get("score", 0) for k, v in (current_cats or {}).items()}


def contributions(roster, now, sunday):
    """Each counted player's projected stats for the rest of the week: season average x games not started yet."""
    rows = []
    for p in filter(plays, roster):
        # games already played are in the current matchup totals; counting them again would double them
        g = sum(1 for x in p.schedule.values() if now < x["date"] and x["date"].date() <= sunday)
        avg = season_avg(p)
        rows.append({"name": p.name, "games": g, **{k: avg.get(k, 0) * g for k in COUNTING + ["FGM", "FGA", "FTM", "FTA"]}})
    return rows


def project(roster, current_cats, now, sunday):
    """Current matchup totals + the sum of contributions(). FG%/FT% use only remaining games."""
    rows = contributions(roster, now, sunday)
    tot = {k: sum(r[k] for r in rows) for k in COUNTING + ["FGM", "FGA", "FTM", "FTA"]}
    current = scores(current_cats)
    out = {k: round(tot[k] + current.get(k, 0), 1) for k in COUNTING}
    # shortcut: ESPN only returns the current %, not makes/attempts, so % ignores games already played.
    out["FG%"] = round(tot["FGM"] / tot["FGA"], 3) if tot["FGA"] else current.get("FG%", 0)
    out["FT%"] = round(tot["FTM"] / tot["FTA"], 3) if tot["FTA"] else current.get("FT%", 0)
    return out


def compare(mine, theirs):
    result = {}
    for c in CATS:
        a, b = mine.get(c, 0), theirs.get(c, 0)
        if c == "TO":
            a, b = -a, -b
        result[c] = "W" if a > b else "L" if a < b else "T"
    return result


def find_my_team(league, team_id, swid):
    for t in league.teams:
        if team_id and str(t.team_id) == str(team_id):
            return t
        if swid and any(o.get("id", "").lower() == swid.lower() for o in t.owners):
            return t
    raise HTTPException(400, "Could not find your team. Set TEAM_ID in .env.")


def tldr(text, limit=180):
    """Strip HTML from ESPN news and keep the first sentence."""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(text or ""))).strip()
    # shortcut: sentence split skips vs./Jr./Sr./St. only; other abbreviations cut early. Use an LLM summary if that bites.
    first = re.split(r"(?<!\bvs\.)(?<!\bJr\.)(?<!\bSr\.)(?<!\bSt\.)(?<=[.!?])\s+(?=[A-Z])", text, maxsplit=1)[0]
    return first if len(first) <= limit else first[:limit].rsplit(" ", 1)[0] + "…"


def upcoming_games(player, today, end):
    games = sorted((g for g in player.schedule.values() if today <= g["date"].date() <= end), key=lambda g: g["date"])
    return [{"date": g["date"].date().isoformat(), "opp": g["team"]} for g in games]


def player_news(league, roster):
    def one(p):
        try:
            feed = league.espn_request.get_player_news(p.playerId).get("news", {}).get("feed", [])
            return p.playerId, [{"headline": tldr(n.get("headline")), "tldr": tldr(n.get("story"))} for n in feed[:3]]
        except Exception:
            return p.playerId, []  # news is nice-to-have; never break the dashboard over it

    # one request per player; run them side by side instead of one after another
    with ThreadPoolExecutor(8) as pool:
        return dict(pool.map(one, roster))


def build_dashboard(league, me, now, news):
    today = now.date()
    monday, sunday = week_bounds(today)
    matchup = next(
        (m for m in league.scoreboard() if me in (m.home_team, m.away_team)), None
    )
    opp, my_cats, opp_cats = None, None, None
    if matchup:
        home = matchup.home_team == me
        opp = matchup.away_team if home else matchup.home_team
        my_cats = matchup.home_team_cats if home else matchup.away_team_cats
        opp_cats = matchup.away_team_cats if home else matchup.home_team_cats
    if not hasattr(opp, "roster"):
        opp = None  # bye week

    def player_row(p):
        return {
            "id": p.playerId,
            "name": p.name,
            "pos": "/".join(s for s in p.eligibleSlots if s in ("PG", "SG", "SF", "PF", "C")),
            "team": p.proTeam,
            "injury": p.injuryStatus if p.injuryStatus not in (None, "ACTIVE") else None,
            "games_left": len(game_dates(p, today, sunday)),
            "upcoming": upcoming_games(p, today, sunday),
            "news": news.get(p.playerId, []),
        }

    data = {
        "league": league.settings.name,
        "team": me.team_name,
        "roster": [player_row(p) for p in me.roster],
        "week": {"start": monday.isoformat(), "today": today.isoformat()},
        "opponent": None,
    }
    data["slate"] = {"mine": daily_slate(me.roster, monday)}
    if opp:
        mine_proj = project(me.roster, my_cats, now, sunday)
        opp_proj = project(opp.roster, opp_cats, now, sunday)
        data["opponent"] = opp.team_name
        data["slate"]["theirs"] = daily_slate(opp.roster, monday)
        data["projection"] = {
            "cats": CATS,
            "mine": mine_proj,
            "theirs": opp_proj,
            "result": compare(mine_proj, opp_proj),
            "current": {"mine": scores(my_cats), "theirs": scores(opp_cats)},
            "players": {"mine": contributions(me.roster, now, sunday), "theirs": contributions(opp.roster, now, sunday)},
        }
    return data


@app.get("/api/dashboard")
def dashboard(refresh: bool = False):
    # Refresh may skip the cache only after 60s, so repeat clicks can't flood ESPN with your cookie
    if _cache["data"] and time.time() - _cache["at"] < (60 if refresh else CACHE_SECONDS):
        return _cache["data"]
    if not os.environ.get("LEAGUE_ID"):
        raise HTTPException(500, "LEAGUE_ID not set. Copy .env.example to .env and fill it in.")
    try:
        league = League(
            league_id=int(os.environ["LEAGUE_ID"]),
            year=current_season(),
            espn_s2=os.environ.get("ESPN_S2") or None,
            swid=os.environ.get("SWID") or None,
        )
    except Exception as e:
        raise HTTPException(502, f"ESPN refused the request ({e}). Check LEAGUE_ID, ESPN_S2 and SWID; the cookies expire.")
    me = find_my_team(league, os.environ.get("TEAM_ID"), os.environ.get("SWID"))
    data = build_dashboard(league, me, datetime.now(), player_news(league, me.roster))
    _cache.update(at=time.time(), data=data)
    return data


def parse_gamelog(data):
    """Regular season + playoff games, newest first, with opponent (fantasy team code) and the 9 league cats."""
    labels, events, games = data.get("labels", []), data.get("events", {}), []
    for season_type in data.get("seasonTypes", []):
        if not season_type.get("displayName", "").endswith(("Regular Season", "Postseason")):
            continue
        for cat in season_type.get("categories", []):
            for row in cat.get("events", []):
                ev = events.get(row.get("eventId"), {})
                abbr = ev.get("opponent", {}).get("abbreviation", "")
                st = dict(zip(labels, row.get("stats", [])))
                games.append({
                    # gameDate is UTC; local (TZ env) date matches the day the game was played
                    "date": datetime.fromisoformat(ev["gameDate"].replace("Z", "+00:00")).astimezone().date().isoformat(),
                    "opp": SITE_TO_FANTASY.get(abbr, abbr),
                    "at": ev.get("atVs", "vs"),
                    "result": f'{ev.get("gameResult", "")} {ev.get("score", "")}'.strip(),
                    "stats": {
                        "FG%": st.get("FG", ""), "FT%": st.get("FT", ""),
                        "3PM": st.get("3PT", "").split("-")[0],
                        "PTS": st.get("PTS", ""), "REB": st.get("REB", ""), "AST": st.get("AST", ""),
                        "STL": st.get("STL", ""), "BLK": st.get("BLK", ""), "TO": st.get("TO", ""),
                    },
                })
    return sorted(games, key=lambda g: g["date"], reverse=True)


def fetch_gamelog(player_id, season):
    key = (player_id, season)
    hit = _gamelogs.get(key)
    if hit and (season < current_season() or time.time() - hit[0] < 3600):
        return hit[1]
    with urlopen(GAMELOG_URL.format(player_id, season), timeout=10) as r:
        games = parse_gamelog(json.load(r))  # raw response is ~3 MB in memory; keep only the parsed games
    _gamelogs[key] = (time.time(), games)
    return games


@app.get("/api/vs/{player_id}/{opp}")
def vs_team(player_id: int, opp: str):
    if opp not in PRO_TEAM_MAP.values():
        raise HTTPException(400, "Unknown team")
    # only your roster, so outsiders can't make the server fetch (and cache) every NBA player
    if player_id not in {p["id"] for p in (_cache["data"] or {}).get("roster", [])}:
        raise HTTPException(404, "Player not on your roster")
    games = []
    for season in range(current_season(), current_season() - H2H_SEASONS, -1):
        try:
            games += [g for g in fetch_gamelog(player_id, season) if g["opp"] == opp]
        except Exception:
            continue  # rookie seasons and missing years 404; skip them
        if len(games) >= 3:
            break
    return {"opp": opp, "seasons": H2H_SEASONS, "games": games[:3]}


@app.get("/")
def index():
    return FileResponse(Path(__file__).with_name("static") / "index.html")


load_env()
