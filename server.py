import html
import json
import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from urllib.request import Request, urlopen

import streaming
from espn_api.basketball import League
from espn_api.basketball.player import Player
from espn_api.basketball.constant import POSITION_MAP, PRO_TEAM_MAP, STATS_MAP
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

COUNTING = ["PTS", "REB", "AST", "STL", "BLK", "3PM", "TO"]
CATS = ["FG%", "FT%", "3PM", "PTS", "REB", "AST", "STL", "BLK", "TO"]
CACHE_SECONDS = 300

app = FastAPI()
_cache = {}  # matchup week (None = current) -> {"at": time, "data": dashboard}
_gamelogs = {}

GAMELOG_URL = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{}/gamelog?season={}"
# ESPN's public site API uses different team codes from the fantasy API
SITE_TO_FANTASY = {"GS": "GSW", "NO": "NOP", "NY": "NYK", "PHI": "PHL", "PHX": "PHO", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS"}
H2H_SEASONS = 4
# ESPN's CDN can refuse Python's default User-Agent from cloud hosts such as Render
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def current_season():
    return int(os.environ.get("YEAR", date.today().year + 1))


def load_env(path=Path(__file__).with_name(".env")):
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def days_between(start, end):
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def week_dates(league, week, today):
    """First and last day of matchup `week` from ESPN's matchup period -> scoring periods map (1 scoring period = 1 day)."""
    periods = (getattr(league.settings, "matchup_periods", None) or {}).get(str(week)) or []
    if len(periods) > 1 and all(isinstance(p, int) for p in periods):
        # scoring period ids are consecutive days; league.scoringPeriodId is today
        return tuple(today + timedelta(days=p - league.scoringPeriodId) for p in (min(periods), max(periods)))
    return week_bounds(today, week - league.currentMatchupPeriod)  # ESPN gave no day list: count Mon-Sun weeks


def week_bounds(today, offset=0):
    """Monday-Sunday of the week `offset` weeks from today's."""
    # shortcut: assumes Mon-Sun matchup weeks; ESPN stretches some weeks (All-Star, opening week), shifting later weeks.
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    return monday, monday + timedelta(days=6)


def game_dates(player, start, end):
    return [g["date"].date() for g in player.schedule.values() if start <= g["date"].date() <= end]


def played(entry, period):
    """The player's real stats in that scoring period (one period = one day), or None if they recorded none."""
    for s in entry.get("playerPoolEntry", {}).get("player", {}).get("stats", []):
        if s.get("scoringPeriodId") == period and s.get("statSourceId", 0) == 0 and any(s.get("stats", {}).values()):
            named = {STATS_MAP.get(k, k): v for k, v in s["stats"].items()}
            return {k: named.get(k, 0) for k in COUNTING + ["FGM", "FGA", "FTM", "FTA"]}
    return None


def day_lineups(league, start, end, today, team_ids, week):
    """Each day's starting lineup per team, from ESPN's per-day rosters: {date: {team_id: {player_id: that day's stats or None}}}."""
    def one(d):
        try:
            period = league.scoringPeriodId + (d - today).days
            data = league.espn_request.league_get(
                params={"view": ["mMatchupScore", "mScoreboard"], "scoringPeriodId": period},
                headers={"x-fantasy-filter": json.dumps({"schedule": {"filterMatchupPeriodIds": {"value": [week]}}})},
            )
        except Exception:
            return d, {}  # a missing day falls back to today's lineup
        teams = {}
        for m in data.get("schedule", []):
            for t in (m.get("home", {}), m.get("away", {})):
                if t.get("teamId") in team_ids and "rosterForCurrentScoringPeriod" in t:
                    teams[t["teamId"]] = {e["playerId"]: played(e, period) for e in t["rosterForCurrentScoringPeriod"].get("entries", [])
                                          if POSITION_MAP.get(e.get("lineupSlotId")) not in ("BE", "IR")}
        return d, teams

    with ThreadPoolExecutor(7) as pool:
        return dict(pool.map(one, days_between(start, end)))


def starting(p, team_id, d, lineups):
    """In the team's starting lineup (not bench or IR) on day d; uses today's lineup if ESPN's lineup for d is missing."""
    day = (lineups or {}).get(d, {}).get(team_id)
    return p.lineupSlot not in ("BE", "IR") if day is None else p.playerId in day


def season_avg(player):
    return player.stats.get(f"{player.year}_total", {}).get("avg") or {}


def daily_slate(roster, team_id, start, end, today, lineups=None):
    """Starters each day: past days count starters who actually played; today on, starters with a game who aren't OUT."""
    counts = []
    for d in days_between(start, end):
        day = (lineups or {}).get(d, {}).get(team_id)
        if d < today and day is not None:
            counts.append(sum(1 for v in day.values() if v))
        else:
            counts.append(sum(1 for p in roster if p.injuryStatus != "OUT" and starting(p, team_id, d, lineups) and game_dates(p, d, d)))
    return counts


def day_state(p, team_id, d, today, lineups=None):
    """One player's status for one day, by the same rules as the slate:
    start (counts), bench, out, dnp (started a past day but recorded no stats) or off (no game)."""
    if not game_dates(p, d, d):
        return "off"
    day = (lineups or {}).get(d, {}).get(team_id)
    if d < today and day is not None:
        return "bench" if p.playerId not in day else "start" if day[p.playerId] else "dnp"
    if p.injuryStatus == "OUT":
        return "out"
    return "start" if starting(p, team_id, d, lineups) else "bench"


def planner_rows(roster, team_id, start, end, today, lineups):
    """Weekly planner rows: each player's slot, injury, season averages and status for every day of the week."""
    keep = COUNTING + ["FGM", "FGA", "FTM", "FTA"]
    days = days_between(start, end)

    def real(p, d):  # actual stats for a day up to today that the player started and played
        stats = (lineups or {}).get(d, {}).get(team_id, {}).get(p.playerId)
        return stats if d <= today and isinstance(stats, dict) else None

    return [{"id": p.playerId, "name": p.name, "team": p.proTeam, "slot": p.lineupSlot,
             "injury": p.injuryStatus if p.injuryStatus not in (None, "ACTIVE") else None,
             "avg": {k: season_avg(p).get(k, 0) for k in keep},
             "days": [day_state(p, team_id, d, today, lineups) for d in days],
             "actual": [real(p, d) for d in days]} for p in roster]


def team_games(league, start, end):
    """Games each NBA team plays in the week, by fantasy team code."""
    counts = Counter()
    for team_id, periods in getattr(league, "pro_schedule", {}).items():
        dates = {datetime.fromtimestamp(g["date"] / 1000).date() for games in periods.values() for g in games}
        counts[PRO_TEAM_MAP.get(team_id, str(team_id))] = sum(start <= d <= end for d in dates)
    return dict(counts)


def nba_games(league, start, end):
    """Number of NBA games each day of the week."""
    days = {d: set() for d in days_between(start, end)}
    for periods in getattr(league, "pro_schedule", {}).values():
        for games in periods.values():
            for g in games:
                d = datetime.fromtimestamp(g["date"] / 1000).date()
                if d in days:
                    days[d].add((g["homeProTeamId"], g["awayProTeamId"]))
    return [len(v) for v in days.values()]


def scores(current_cats):
    return {k: (v or {}).get("score", 0) for k, v in (current_cats or {}).items()}


def contributions(roster, now, start, end, team_id=None, lineups=None):
    """Each starter's projected stats for the rest of the week: season average x starts not played yet. OUT players skip."""
    rows = []
    for p in roster:
        if p.injuryStatus == "OUT":
            continue
        # games already played are in the current matchup totals; counting them again would double them
        g = sum(1 for x in p.schedule.values()
                if now < x["date"] and start <= x["date"].date() <= end and starting(p, team_id, x["date"].date(), lineups))
        if not g:
            continue
        avg = season_avg(p)
        rows.append({"name": p.name, "games": g, **{k: avg.get(k, 0) * g for k in COUNTING + ["FGM", "FGA", "FTM", "FTA"]}})
    return rows


def project(roster, current_cats, now, start, end, team_id=None, lineups=None):
    """Current matchup totals + the sum of contributions(). FG%/FT% use only remaining games."""
    rows = contributions(roster, now, start, end, team_id, lineups)
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


def matchup_sides(league, me, week):
    """(opponent team or None on a bye, your category scores, theirs) for matchup `week`."""
    matchup = next((m for m in league.scoreboard(week) if me in (m.home_team, m.away_team)), None)
    if not matchup:
        return None, None, None
    home = matchup.home_team == me
    opp = matchup.away_team if home else matchup.home_team
    my_cats = matchup.home_team_cats if home else matchup.away_team_cats
    opp_cats = matchup.away_team_cats if home else matchup.home_team_cats
    return (opp if hasattr(opp, "roster") else None), my_cats, opp_cats


def build_dashboard(league, me, now, news, week=None):
    today = now.date()
    current = league.currentMatchupPeriod
    week = week or current
    first, last = week_dates(league, week, today)
    left_from = max(today, first)  # games still to come in the selected week (none for a past week)
    opp, my_cats, opp_cats = matchup_sides(league, me, week)

    def player_row(p):
        return {
            "id": p.playerId,
            "name": p.name,
            "pos": "/".join(s for s in p.eligibleSlots if s in ("PG", "SG", "SF", "PF", "C")),
            "team": p.proTeam,
            "injury": p.injuryStatus if p.injuryStatus not in (None, "ACTIVE") else None,
            "games_left": len(game_dates(p, left_from, last)),
            "upcoming": upcoming_games(p, left_from, last),
            "news": news.get(p.playerId, []),
        }

    data = {
        "league": league.settings.name,
        "team": me.team_name,
        "roster": [player_row(p) for p in me.roster],
        "week": {"start": first.isoformat(), "end": last.isoformat(), "today": today.isoformat(), "number": week, "current": current,
                 "count": max([week, current, *map(int, getattr(league.settings, "matchup_periods", None) or {})])},
        "opponent": None,
    }
    lineups = day_lineups(league, first, last, today, {me.team_id} | ({opp.team_id} if opp else set()), week)
    fill_actuals(lineups, today)
    data["slate"] = {"mine": daily_slate(me.roster, me.team_id, first, last, today, lineups), "nba": nba_games(league, first, last)}
    data["planner"] = {"mine": planner_rows(me.roster, me.team_id, first, last, today, lineups),
                       "teams": team_games(league, first, last)}
    if opp:
        mine_proj = project(me.roster, my_cats, now, first, last, me.team_id, lineups)
        opp_proj = project(opp.roster, opp_cats, now, first, last, opp.team_id, lineups)
        data["opponent"] = opp.team_name
        data["slate"]["theirs"] = daily_slate(opp.roster, opp.team_id, first, last, today, lineups)
        data["planner"]["theirs"] = planner_rows(opp.roster, opp.team_id, first, last, today, lineups)
        data["projection"] = {
            "cats": CATS,
            "mine": mine_proj,
            "theirs": opp_proj,
            "result": compare(mine_proj, opp_proj),
            "current": {"mine": scores(my_cats), "theirs": scores(opp_cats),
                        "result": compare(scores(my_cats), scores(opp_cats))},  # who leads each category right now
            "players": {"mine": contributions(me.roster, now, first, last, me.team_id, lineups),
                        "theirs": contributions(opp.roster, now, first, last, opp.team_id, lineups)},
        }
    return data


def connect():
    """Log in to the ESPN league and find your team; clear 5xx errors when that fails."""
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
    return league, find_my_team(league, os.environ.get("TEAM_ID"), os.environ.get("SWID"))


def game_lines(p, team_id, lineups, now, first, last):
    """{date: season-average line} for p's games not started yet this week; with a team_id, only days p starts."""
    if p.injuryStatus == "OUT":
        return {}
    avg = season_avg(p)
    line = {k: avg.get(k, 0) for k in streaming.KEYS}
    return {x["date"].date(): line for x in p.schedule.values()
            if now < x["date"] and first <= x["date"].date() <= last
            and (team_id is None or starting(p, team_id, x["date"].date(), lineups))}


def free_agents(league, size=60):
    """Most-owned free agents (not waivers: those can't be added the same day), with their NBA schedule attached."""
    filters = {"players": {"filterStatus": {"value": ["FREEAGENT"]}, "limit": size,
                           "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}}
    data = league.espn_request.league_get(params={"view": "kona_player_info", "scoringPeriodId": league.scoringPeriodId},
                                          headers={"x-fantasy-filter": json.dumps(filters)})
    # espn_api's own free_agents() leaves out the schedule, which the planner needs
    return [Player(p, league.year, league.pro_schedule) for p in data.get("players", [])]


def acquisitions_left(league, me, first, last, week):
    """Weekly add limit, adds used this matchup and adds left, plus what they were read from so the page can show it.
    Used = your free-agent and waiver adds dated inside the matchup, from ESPN's league activity log."""
    limit = league.settings.matchup_acquisition_limit
    limit = limit if limit and limit > 0 else None
    # ESPN's "limit per scoring period" flag: the limit is per day (1 scoring period = 1 day), not per matchup
    daily = limit if limit and getattr(league.settings, "matchup_limit_per_scoring_period", False) else None
    info = {"limit": daily * len(days_between(first, last)) if daily else limit, "daily_limit": daily, "used": None, "left": None,
            "counted": [], "recent": [], "counter": None, "errors": []}
    try:
        # shortcut: reads the league's 100 latest moves; a very busy league could push older adds this week out
        mine = [{"date": datetime.fromtimestamp(a.date / 1000).date().isoformat(), "action": action, "player": str(player)}
                for a in league.recent_activity(size=100) for team, action, player, *_ in a.actions
                if getattr(team, "team_id", None) == me.team_id and action in ("FA ADDED", "WAIVER ADDED")]
        info["counted"] = [m for m in mine if first.isoformat() <= m["date"] <= last.isoformat()]
        info["recent"] = mine[:5]  # your latest adds whatever their date, to check the week's dates
        info["used"] = len(info["counted"])
    except Exception as e:
        info["errors"].append(f"activity log: {e}")
    try:  # ESPN's own per-matchup counter, shown next to our count for comparison
        teams = league.espn_request.league_get(params={"view": "mTeam"}).get("teams", [])
        counter = next(t for t in teams if t["id"] == me.team_id).get("transactionCounter", {})
        info["counter"] = {"this_week": counter.get("matchupAcquisitionTotals", {}).get(str(week)),
                           "by_week": counter.get("matchupAcquisitionTotals"), "season": counter.get("acquisitions")}
    except Exception as e:
        info["errors"].append(f"team counter: {e}")
    if info["limit"] is not None and info["used"] is not None:
        info["left"] = max(0, info["limit"] - info["used"])
    return info


@app.get("/api/stream")
def stream(drops: Annotated[str, Query(max_length=400)] = "", acq: Annotated[int | None, Query(ge=0, le=20)] = None,
           plan: bool = False):
    """Your roster and acquisitions left; with plan=true, the best week of free-agent pickups."""
    league, me = connect()
    now = datetime.now()
    today, week = now.date(), league.currentMatchupPeriod
    first, last = week_dates(league, week, today)
    opp, my_cats, opp_cats = matchup_sides(league, me, week)
    lineups = day_lineups(league, first, last, today, {me.team_id} | ({opp.team_id} if opp else set()), week)
    acquisitions = acquisitions_left(league, me, first, last, week)
    out = {"week": {"number": week, "start": first.isoformat(), "end": last.isoformat()},
           "opponent": opp.team_name if opp else None,
           "acquisitions": acquisitions,
           "roster": [{"id": p.playerId, "name": p.name, "team": p.proTeam, "slot": p.lineupSlot,
                       "injury": p.injuryStatus if p.injuryStatus not in (None, "ACTIVE") else None,
                       "starts_left": len(game_lines(p, me.team_id, lineups, now, first, last))} for p in me.roster]}
    if not (plan and opp):
        return out
    roster_ids = {p.playerId for p in me.roster}
    droppable = {int(x) for x in drops.split(",") if x.strip().isdigit()} & roster_ids
    opp_players = [{"id": p.playerId, "name": p.name, "games": game_lines(p, opp.team_id, lineups, now, first, last)} for p in opp.roster]
    out["plan"] = streaming.plan(
        days=days_between(max(today, first), last),
        slots=sum(1 for p in me.roster if p.lineupSlot not in ("BE", "IR")),  # your starting spots
        mine=[{"id": p.playerId, "name": p.name, "games": game_lines(p, me.team_id, lineups, now, first, last)} for p in me.roster],
        opp_totals=streaming.totals(scores(opp_cats), opp_players),
        current=scores(my_cats),
        free_agents=[{"id": p.playerId, "name": f"{p.name} ({p.proTeam})", "lines": game_lines(p, None, lineups, now, first, last)}
                     for p in free_agents(league)],
        droppable=droppable,
        acquisitions=acq if acq is not None else (acquisitions["left"] or 0),
        # daily limit minus adds already made that day (today's count matters; past days aren't planned)
        day_caps={d: max(0, acquisitions["daily_limit"] - sum(c["date"] == d.isoformat() for c in acquisitions["counted"]))
                  for d in days_between(max(today, first), last)} if acquisitions["daily_limit"] else None,
    )
    out["plan"]["opp_current"] = scores(opp_cats)
    out["plan"]["opp_players"] = streaming.rows(opp_players)
    return out


@app.get("/api/dashboard")
def dashboard(refresh: bool = False, week: Annotated[int | None, Query(ge=1, le=30)] = None):
    # Refresh may skip the cache only after 60s, so repeat clicks can't flood ESPN with your cookie
    hit = _cache.get(week)
    if hit and time.time() - hit["at"] < (60 if refresh else CACHE_SECONDS):
        return hit["data"]
    league, me = connect()
    data = build_dashboard(league, me, datetime.now(), player_news(league, me.roster), week)
    data["updated"] = datetime.now(timezone.utc).isoformat()  # when ESPN was last pulled; the browser shows it in local time
    _cache[week] = {"at": time.time(), "data": data}
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


def stat_line(game):
    """A parsed game-log entry as numbers, with the same keys as season averages."""
    st = game["stats"]
    num = lambda v: float(v) if v not in ("", "-", None) else 0.0
    fgm, fga = (num(x) for x in (st.get("FG%") or "0-0").split("-"))
    ftm, fta = (num(x) for x in (st.get("FT%") or "0-0").split("-"))
    return {"FGM": fgm, "FGA": fga, "FTM": ftm, "FTA": fta, **{k: num(st.get(k)) for k in COUNTING}}


def fill_actuals(lineups, today):
    """Put each starter's real stats from ESPN's game log into the daily lineups, for days up to today."""
    days = {d: teams for d, teams in (lineups or {}).items() if d <= today}
    ids = {pid for teams in days.values() for t in teams.values() for pid in t}

    def logs(pid):
        try:
            return pid, {g["date"]: stat_line(g) for g in fetch_gamelog(pid, current_season())}
        except Exception:
            return pid, {}  # keep whatever the lineup data said

    with ThreadPoolExecutor(8) as pool:
        by_player = dict(pool.map(logs, ids))
    for d, teams in days.items():
        for t in teams.values():
            for pid in t:
                line = by_player.get(pid, {}).get(d.isoformat())
                if line:
                    t[pid] = line


def fetch_gamelog(player_id, season):
    key = (player_id, season)
    hit = _gamelogs.get(key)
    if hit and (season < current_season() or time.time() - hit[0] < 600):
        return hit[1]
    with urlopen(Request(GAMELOG_URL.format(player_id, season), headers=BROWSER_UA), timeout=10) as r:
        games = parse_gamelog(json.load(r))  # raw response is ~3 MB in memory; keep only the parsed games
    _gamelogs[key] = (time.time(), games)
    return games


@app.get("/api/vs/{player_id}/{opp}")
def vs_team(player_id: int, opp: str):
    if opp not in PRO_TEAM_MAP.values():
        raise HTTPException(400, "Unknown team")
    # only your roster, so outsiders can't make the server fetch (and cache) every NBA player
    if player_id not in {p["id"] for hit in _cache.values() for p in hit["data"].get("roster", [])}:
        raise HTTPException(404, "Player not on your roster")
    games, errors = [], []
    for season in range(current_season(), current_season() - H2H_SEASONS, -1):
        try:
            games += [g for g in fetch_gamelog(player_id, season) if g["opp"] == opp]
        except Exception as e:
            errors.append(f"{season}: {e}")
            continue  # rookie seasons and missing years 404; skip them
        if len(games) >= 3:
            break
    if not games and errors and errors[0].startswith(f"{current_season()}:"):
        # even this season failed, so this is ESPN being unreachable, not a player without games
        raise HTTPException(502, "ESPN game log unavailable (" + "; ".join(errors) + ")")
    return {"opp": opp, "seasons": H2H_SEASONS, "games": games[:3]}


@app.get("/")
def index():
    return FileResponse(Path(__file__).with_name("static") / "index.html")


load_env()
