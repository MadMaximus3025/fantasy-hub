import html
import logging
import json
import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from urllib.request import Request, urlopen

import backtest  # backtest.site() = the site's projection model, replayed on game logs
import streaming
from espn_api.basketball import League
from espn_api.basketball.player import Player
from espn_api.basketball.constant import POSITION_MAP, PRO_TEAM_MAP, STATS_MAP
from fastapi import FastAPI, HTTPException, Query, Request as HTTPRequest
from fastapi.responses import FileResponse, JSONResponse

COUNTING = ["PTS", "REB", "AST", "STL", "BLK", "3PM", "TO"]
CATS = ["FG%", "FT%", "3PM", "PTS", "REB", "AST", "STL", "BLK", "TO"]
CACHE_SECONDS = 300

REFRESH_SECONDS = 240  # background rebuild of the current week, inside the 5-minute cache
BACKGROUND = True  # tests set False to run builds inline


def keep_fresh():
    """While the server is up, rebuild the current week every few minutes so visits get saved data instantly."""
    while True:
        if os.environ.get("LEAGUE_ID"):
            start_build(None)
        time.sleep(REFRESH_SECONDS)


@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=keep_fresh, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)


@app.exception_handler(Exception)
def server_error(request: HTTPRequest, exc: Exception):
    """Unexpected errors: full traceback in the server log, a readable reason on the page instead of a bare 500."""
    logging.exception("Unhandled error on %s", request.url.path)
    return JSONResponse({"detail": f"Server error ({type(exc).__name__}: {exc})"}, status_code=500)
_cache = {}  # matchup week (None = current) -> {"at": time, "data": dashboard, "partial": no news/actuals yet}
_league = {"at": 0, "value": None}  # ESPN login + league data, shared by every page for CACHE_SECONDS
_login_lock = threading.Lock()  # one ESPN login at a time; concurrent visits wait and reuse it
_building = set()  # weeks with a full build running
_building_lock = threading.Lock()
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
        return tuple(period_date(league, p, today) for p in (min(periods), max(periods)))
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


_periods = {}  # id of a league's pro schedule -> {date: ESPN scoring period}


def period_map(league):
    """{date: ESPN scoring period} for every day with NBA games, from ESPN's schedule (games are keyed by period)."""
    sched = getattr(league, "pro_schedule", None) or {}
    key = id(sched)
    if key not in _periods:
        _periods.clear()  # one league per server; keep only the latest schedule's map
        _periods[key] = {datetime.fromtimestamp(g["date"] / 1000).date(): int(period)
                         for games in sched.values() for period, gs in games.items() for g in gs}
    return _periods[key]


def scoring_period(league, d, today):
    """ESPN's scoring period for date d. ESPN's current period (league.scoringPeriodId) rolls over some hours after
    midnight, so it can't stand in for "today". A day without games counts on from the nearest earlier day that has
    some; no schedule at all: offset from today."""
    known = period_map(league)
    before = max((x for x in known if x <= d), default=None)
    if before is None:
        return league.scoringPeriodId + (d - today).days
    return known[before] + (d - before).days


def period_date(league, period, today):
    """The date of ESPN scoring period `period` (the inverse of scoring_period)."""
    by_period = {v: k for k, v in period_map(league).items()}
    before = max((x for x in by_period if x <= period), default=None)
    if before is None:
        return today + timedelta(days=period - league.scoringPeriodId)
    return by_period[before] + timedelta(days=period - before)


def live_days(league, lineups, today):
    """Days up to today whose stats ESPN hasn't put into the matchup totals yet: its current period and later."""
    current = getattr(league, "scoringPeriodId", None)
    if current is None:
        return [d for d in (lineups or {}) if d == today]
    return [d for d in (lineups or {}) if d <= today and scoring_period(league, d, today) >= current]


def day_lineups(league, start, end, today, team_ids, week):
    """Each day's starting lineup per team, from ESPN's per-day rosters: {date: {team_id: {player_id: that day's stats or None}}},
    and each day's full roster entries {date: {team_id: {player_id: entry}}} (future days include pending adds and drops)."""
    def one(d):
        try:
            period = scoring_period(league, d, today)
            data = league.espn_request.league_get(
                params={"view": ["mMatchupScore", "mScoreboard"], "scoringPeriodId": period},
                headers={"x-fantasy-filter": json.dumps({"schedule": {"filterMatchupPeriodIds": {"value": [week]}}})},
            )
        except Exception:
            return d, {}, {}  # a missing day falls back to today's lineup
        teams, rosters = {}, {}
        for m in data.get("schedule", []):
            for t in (m.get("home", {}), m.get("away", {})):
                if t.get("teamId") in team_ids and "rosterForCurrentScoringPeriod" in t:
                    entries = t["rosterForCurrentScoringPeriod"].get("entries", [])
                    teams[t["teamId"]] = {e["playerId"]: played(e, period) for e in entries
                                          if POSITION_MAP.get(e.get("lineupSlotId")) not in ("BE", "IR")}
                    rosters[t["teamId"]] = {e["playerId"]: e for e in entries}
        return d, teams, rosters

    with ThreadPoolExecutor(7) as pool:
        days = list(pool.map(one, days_between(start, end)))
    return {d: t for d, t, _ in days}, {d: r for d, _, r in days}


def pending_moves(team, rosters, today, league):
    """Adds and drops ESPN holds until a later day (made after that day's games started). An added player joins the
    roster from the day he arrives; a dropped one is marked. Per-day lineups already decide which days each counts."""
    ids = {p.playerId for p in team.roster}
    for d in sorted(x for x in rosters if x > today):
        day = rosters[d].get(team.team_id)
        if day is None:
            continue  # ESPN didn't return that day
        for p in team.roster:
            if p.playerId not in day and not hasattr(p, "pending_until"):
                p.pending_until, p.pending = d - timedelta(days=1), f"Dropped after {'today' if d - timedelta(days=1) == today else (d - timedelta(days=1)).strftime('%a')}"
        for pid, entry in day.items():
            if pid in ids:
                continue
            try:
                p = Player(entry, league.year, league.pro_schedule)
            except Exception:
                logging.exception("Couldn't read a pending add")
                continue
            p.pending_from, p.pending = d, f"Added · from {d.strftime('%a')}"
            team.roster.append(p)
            ids.add(pid)


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
    start (counts), bench, out, dnp (started a past day but recorded no stats) or off (no game, or not on the team)."""
    if not game_dates(p, d, d) or d < getattr(p, "pending_from", d) or d > getattr(p, "pending_until", d):
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
             "injury": p.injuryStatus if p.injuryStatus not in (None, "ACTIVE") else None, "pending": getattr(p, "pending", None),
             "avg": {k: proj_avg(p).get(k, 0) for k in keep},  # projection (what every page uses)
             "season": {k: season_avg(p).get(k, 0) for k in keep}, "factor": getattr(p, "proj_factor", 1), "why": getattr(p, "proj_why", ""),
             "days": [day_state(p, team_id, d, today, lineups) for d in days],
             "actual": [real(p, d) for d in days],
             "opps": [next((g["team"] for g in p.schedule.values() if g["date"].date() == d), None) for d in days]}
            for p in roster]


def add_impact(rows):
    """Per-game impact for the planner: each started day's line (actual, else season average) scored across the 9
    categories against both rosters' averages, the week's total, and each player's two strongest categories."""
    if not rows:
        return
    z = streaming.zscorer({i: r["avg"] for i, r in enumerate(rows)})
    for r in rows:
        r["impact_cats"] = [{c: round(v, 2) for c, v in z(r["actual"][i] or r["avg"]).items()} if s == "start" else None
                            for i, s in enumerate(r["days"])]  # per-category impact, for the player panel
        r["impact"] = [round(sum(cats.values()), 1) if cats else None for cats in r["impact_cats"]]
        r["impact_week"] = round(sum(v for v in r["impact"] if v is not None), 1)
        r["top"] = [c for c, _ in sorted(z(r["avg"]).items(), key=lambda kv: -kv[1])[:2]]


def team_games(league, start, end):
    """Each NBA team's games per day of the week (1/0 per day), by fantasy team code; the page sums the days picked."""
    days = days_between(start, end)
    out = {}
    for team_id, periods in getattr(league, "pro_schedule", {}).items():
        dates = {datetime.fromtimestamp(g["date"] / 1000).date() for games in periods.values() for g in games}
        out[PRO_TEAM_MAP.get(team_id, str(team_id))] = [int(d in dates) for d in days]
    return out


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


def with_live(current_cats, lineups, days, team_id):
    """ESPN's matchup totals cover finished scoring periods only; ESPN's live view also counts the period in progress.
    Add the starters' stats of `days` (live_days(): ESPN's current period up to today), games in progress included.
    FG%/FT% have no makes/attempts in the totals: weight ESPN's % by the earlier days' attempts instead."""
    now = [s for d in days for s in ((lineups or {}).get(d, {}).get(team_id) or {}).values() if s]
    if not current_cats or not now:
        return current_cats
    add = lambda k, rows: sum(s.get(k, 0) or 0 for s in rows)
    first = min(days)
    earlier = [s for d, teams in lineups.items() if d < first for s in (teams.get(team_id) or {}).values() if s]
    out = {k: dict(v or {}) for k, v in current_cats.items()}
    for k in COUNTING:
        if k in out:
            out[k]["score"] = (out[k].get("score") or 0) + add(k, now)
    for pct, m, a in (("FG%", "FGM", "FGA"), ("FT%", "FTM", "FTA")):
        if pct not in out or not add(a, now):
            continue
        board, before = out[pct].get("score") or 0, add(a, earlier)
        if before or not board:  # shortcut: if earlier days are missing but ESPN has a %, keep ESPN's % unchanged
            out[pct]["score"] = (board * before + add(m, now)) / (before + add(a, now))
    return out


def starts_left(p, now, start, end, team_id=None, lineups=None):
    """Games p starts this week that haven't begun (played ones are in the current matchup totals already)."""
    return sum(1 for x in p.schedule.values()
               if now < x["date"] and start <= x["date"].date() <= end and starting(p, team_id, x["date"].date(), lineups))


def availability(roster, now, start, end, team_id=None, lineups=None):
    """Injured or questionable players with starts left: their remaining line and whether the projection counts them
    (OUT players don't; day-to-day ones do), so the page can flip each one between plays and sits."""
    rows = []
    for p in roster:
        if p.injuryStatus in (None, "", "ACTIVE", "NORMAL"):
            continue
        g = starts_left(p, now, start, end, team_id, lineups)
        if g:
            avg = proj_avg(p)
            rows.append({"id": p.playerId, "name": p.name, "status": p.injuryStatus, "games": g, "counted": p.injuryStatus != "OUT",
                         "line": {k: avg.get(k, 0) * g for k in COUNTING + ["FGM", "FGA", "FTM", "FTA"]}})
    return rows


def contributions(roster, now, start, end, team_id=None, lineups=None):
    """Each starter's projected stats for the rest of the week: season average x starts not played yet. OUT players skip."""
    rows = []
    for p in roster:
        if p.injuryStatus == "OUT":
            continue
        g = starts_left(p, now, start, end, team_id, lineups)
        if not g:
            continue
        avg = proj_avg(p)
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


# shortcut: keyword matching, so unusual wording can be missed or mislabeled; switch to an LLM classifier if that bites
INJURY_WORDS = re.compile(
    r"\b(injur\w*|questionable|doubtful|probable|day-to-day|game-time decision|sidelined|ruled out|will miss|"
    r"(miss|missed|misses|missing) (\w+ )?(game|games|time|action|contest)|out (for|with|indefinitely|tonight|again)|"
    r"surgery|sprain\w*|strain\w*|sore\w*|illness|concussion|protocol|fracture\w*|torn|tear|"
    r"(ankle|knee|hamstring|groin|foot|wrist|shoulder|calf|hip|achilles|back) (injury|issue|soreness|tightness|contusion)|"
    r"return(s|ed|ing)? (to action|from)|rest(ed|ing)?|load management|minutes restriction|restriction)\b", re.I)
ROLE_WORDS = re.compile(
    r"\b(starting lineup|start(s|ed|ing)? (in place|for|at|in)|(will|to) start|first start|moved to the bench|"
    r"off the bench|(to|from|on) the bench|benched|depth chart|playing time|sixth man|sixth-man|"
    r"(more|fewer|increased|reduced|limited|extended|expanded|bigger|smaller|larger|heavier|lighter) (minutes|role|workload)|"
    r"(starting|reserve|bench) role|role (expand|grow|shrink|increas|decreas|chang)\w*|minutes (restriction|limit|cap|load)|"
    r"(out of|into|in) the rotation|rotation (spot|role|change)|demot\w*|promot\w*|DNP|did not play|coach'?s decision)\b", re.I)


ROLE_UP = re.compile(
    r"\b((will|to|set to|expected to|could|should) start|first start|start(s|ing)? in place of|"
    r"(join|joins|move[sd]? into|enters?|re-?enters?) the starting lineup|"
    r"(more|increased|extended|expanded|bigger|larger|heavier|extra) (minutes|role|workload|playing time|run)|"
    r"role (expand|grow|increas)\w*|promot\w*)\b", re.I)
ROLE_DOWN = re.compile(
    r"\b(moved to the bench|off the bench|(to|from) the bench|benched|reserve role|"
    r"(fewer|reduced|limited|smaller|lighter|decreased) (minutes|role|workload|playing time)|role (shrink|decreas)\w*|"
    r"minutes (restriction|limit|cap)|out of the rotation|demot\w*|DNP|did not play|coach'?s decision)\b", re.I)


def role_direction(text):
    """+1 if the news says the player's role or minutes go up, -1 if down, 0 if it doesn't say."""
    up, down = ROLE_UP.search(text), ROLE_DOWN.search(text)
    return 1 if up and not down else -1 if down and not up else 0


DAY_WORDS = re.compile(r"\b(tonight|today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def local_time(stamp):
    """ESPN's UTC timestamp ('...Z') as a naive local (server TZ) datetime."""
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)


def role_until(text, published):
    """Last day a role item applies to, or None when it doesn't name a day. 'X will start Wednesday' is about that one
    game, so it stops counting once Wednesday has passed; 'X moves into the starting lineup' keeps counting."""
    sentence = next((s for s in re.split(r"(?<=[.!?])\s+", text) if ROLE_UP.search(s) or ROLE_DOWN.search(s)), "")
    day = DAY_WORDS.search(sentence)
    if not (day and published):
        return None
    pub, word = local_time(published).date(), day.group(1).lower()
    if word in ("tonight", "today"):
        return pub
    if word == "tomorrow":
        return pub + timedelta(days=1)
    return pub + timedelta(days=(WEEKDAYS.index(word) - pub.weekday()) % 7)


def role_factor(p, news, now, last_game=None):
    """(multiplier, reasons) from the newest role news of the last 7 days: x1.2 for a bigger role, x0.75 for a smaller one.
    News about one game that's already been played ('will start Wednesday', now Friday) is skipped: projections look ahead.
    So is news from before his last game (`last_game`, an ISO date): that game is already in the recent-games projection."""
    factor, why = 1.0, []
    for n in news:
        if not (n.get("role") and n.get("published")):  # undated news: age unknown, so it doesn't move the projection
            continue
        if last_game and local_time(n["published"]).date().isoformat() <= last_game:
            continue
        if n.get("until") and n["until"] < now.date().isoformat() and not n.get("while_out"):
            continue
        if n.get("while_out") or (now - local_time(n["published"])).days <= 7:  # replacement starts last until he's back
            factor *= 1.2 if n["role"] > 0 else 0.75
            why.append(f"news: {n['headline']}" + (f" ({n['while_out']} still out)" if n.get("while_out") else ""))
            break
    return factor, why


def game_logs(players):
    """Each player's finished games this season, oldest first, as stat lines with date and opponent; [] if ESPN fails."""
    today = datetime.now().date().isoformat()

    def one(p):
        try:
            log = fetch_gamelog(p.playerId, current_season())
        except Exception:
            return p.playerId, []  # falls back to the season average
        played = [g for g in log if g["date"] < today and (g["stats"].get("MIN") or "0") not in ("0", "", "-")]
        return p.playerId, [stat_line(g) | {"date": g["date"], "opp": g["opp"], "at": g["at"]} for g in reversed(played)]

    with ThreadPoolExecutor(8) as pool:
        return dict(pool.map(one, {p.playerId: p for p in players}.values()))


def project_players(players, news, now, logs=None):
    """The site's one projection recipe, stored on each player for every page to use: per-game line =
    recent-games blend (backtest.site: 60% season / 40% last-7-games per-minute rates x minutes in his last 3 games)
    x role-news factor for news newer than his last game. Without a game log (ESPN down), the ESPN season average."""
    for p in players:
        log = (logs or {}).get(p.playerId) or []
        factor, why = role_factor(p, news.get(p.playerId, []), now, log[-1]["date"] if log else None)
        avg = backtest.site(log) if log else season_avg(p)
        p.proj, p.proj_factor, p.proj_why = {k: avg.get(k, 0) * factor for k in streaming.KEYS}, round(factor, 2), "; ".join(why)
        p.proj_basis = "recent" if log else "season"
        p.recent = [{k: g[k] for k in ("date", "opp", "at", "MIN", *streaming.KEYS)} for g in log[-3:][::-1]]  # newest first


def proj_avg(p):
    """A player's projected per-game line (season average when project_players hasn't run for him)."""
    return getattr(p, "proj", None) or {k: season_avg(p).get(k, 0) for k in streaming.KEYS}


def news_kind(text):
    """'Injury' or 'Playing time' when a news item matters for whether or how much a player plays; None otherwise."""
    return "Injury" if INJURY_WORDS.search(text) else "Playing time" if ROLE_WORDS.search(text) else None


_news = {}  # player id -> (time, items); shared by the dashboard and the streaming planner for 10 minutes


def player_news(league, roster):
    """Latest 3 news items per player that are about injuries or playing time (game recaps and stat lines left out)."""
    def one(p):
        hit = _news.get(p.playerId)
        if hit and time.time() - hit[0] < 600:
            return p.playerId, hit[1]
        try:
            feed = league.espn_request.get_player_news(p.playerId).get("news", {}).get("feed", [])
        except Exception:
            return p.playerId, []  # news is nice-to-have; never break the dashboard over it
        items = []
        for n in feed:
            text = tldr(n.get("headline"), 10_000) + " " + tldr(n.get("story"), 10_000)  # whole story, not just the shown sentence
            if news_kind(text):
                items.append({"headline": tldr(n.get("headline")), "tldr": tldr(n.get("story")), "kind": news_kind(text),
                              "role": role_direction(text), "published": n.get("published") or "",
                              "until": str(role_until(text, n.get("published")) or "")})
                items[-1]["while_out"] = filling_in(p, items[-1], text)
        _news[p.playerId] = (time.time(), items[:3])
        return p.playerId, items[:3]

    try:
        out_now = injured_out()
    except Exception:
        out_now = set()  # without the injury list, replacement starts expire like any one-game news

    def filling_in(p, item, text):
        """The OUT player a bigger-role item is about (e.g. 'starts in place of the injured Jones'), while he's still out."""
        if item["role"] > 0:
            return next((n for n in out_now if n != p.name and n in text), None)

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


def build_dashboard(league, me, now, news, week=None, actuals=True):
    today = now.date()
    current = league.currentMatchupPeriod
    week = week or current
    first, last = week_dates(league, week, today)
    left_from = max(today, first)  # games still to come in the selected week (none for a past week)
    opp, my_cats, opp_cats = matchup_sides(league, me, week)
    lineups, rosters = day_lineups(league, first, last, today, {me.team_id} | ({opp.team_id} if opp else set()), week)
    for team in (me, opp) if opp else (me,):  # pending adds/drops first, then the same roster order on every page
        pending_moves(team, rosters, today, league)
        league_order(team.roster, today)
    # recent games need one game log per player; the quick first version uses season averages until the full build
    logs = game_logs(me.roster + (opp.roster if opp else [])) if actuals else {}
    project_players(me.roster, news, now, logs)
    if opp:
        # opponent gets the same recipe, role news included; the quick first version skips news for both teams
        project_players(opp.roster, player_news(league, opp.roster) if actuals else {}, now, logs)

    def player_row(p):
        return {
            "id": p.playerId,
            "name": p.name,
            "pos": "/".join(s for s in p.eligibleSlots if s in ("PG", "SG", "SF", "PF", "C")),
            "team": p.proTeam,
            "injury": p.injuryStatus if p.injuryStatus not in (None, "ACTIVE") else None,
            "pending": getattr(p, "pending", None),
            # a pending add only plays from the day he arrives; a pending drop stops after his last day
            "games_left": len(game_dates(p, max(left_from, getattr(p, "pending_from", left_from)), min(last, getattr(p, "pending_until", last)))),
            "upcoming": upcoming_games(p, max(left_from, getattr(p, "pending_from", left_from)), min(last, getattr(p, "pending_until", last))),
            "news": news.get(p.playerId, []),
        }

    data = {
        "league": league.settings.name,
        "team": me.team_name,
        "roster": [player_row(p) for p in me.roster],
        "week": {"start": first.isoformat(), "end": last.isoformat(), "today": today.isoformat(), "number": week, "current": current,
                 "count": max([week, current, *map(int, getattr(league.settings, "matchup_periods", None) or {})]),
                 # ESPN's current scoring period vs the one its schedule gives today; they differ until ESPN rolls over
                 "periods": {"espn": getattr(league, "scoringPeriodId", None),
                             "today": scoring_period(league, today, today) if hasattr(league, "scoringPeriodId") else None}},
        "opponent": None,
    }
    if actuals:  # one game-log request per starter; the quick first version skips it
        fill_actuals(lineups, today)
    # match ESPN's live totals: finished days plus today's games, in progress included
    live = live_days(league, lineups, today)
    my_cats = with_live(my_cats, lineups, live, me.team_id)
    opp_cats = with_live(opp_cats, lineups, live, opp.team_id) if opp else opp_cats
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
            "availability": {"mine": availability(me.roster, now, first, last, me.team_id, lineups),
                             "theirs": availability(opp.roster, now, first, last, opp.team_id, lineups)},
        }
    add_impact(data["planner"]["mine"] + data["planner"].get("theirs", []))  # scored against both rosters together
    return data


def connect(fresh=False):
    """ESPN league + your team, reused for CACHE_SECONDS (fresh=True logs in again); clear 5xx errors on failure."""
    if not os.environ.get("LEAGUE_ID"):
        raise HTTPException(500, "LEAGUE_ID not set. Copy .env.example to .env and fill it in.")
    with _login_lock:
        if not fresh and _league["value"] and time.time() - _league["at"] < CACHE_SECONDS:
            return _league["value"]
        try:
            league = League(
                league_id=int(os.environ["LEAGUE_ID"]),
                year=current_season(),
                espn_s2=os.environ.get("ESPN_S2") or None,
                swid=os.environ.get("SWID") or None,
            )
        except Exception as e:
            raise HTTPException(502, f"ESPN refused the request ({e}). Check LEAGUE_ID, ESPN_S2 and SWID; the cookies expire.")
        value = (league, find_my_team(league, os.environ.get("TEAM_ID"), os.environ.get("SWID")))
        _league.update(at=time.time(), value=value)
        return value


def stamped(data):
    data["updated"] = datetime.now(timezone.utc).isoformat()  # when ESPN was last pulled; the browser shows it in local time
    return data


def build_full(week, fresh=False):
    """Everything for a week, news and actual game stats included, saved to the cache."""
    try:
        league, me = connect(fresh)
        data = stamped(build_dashboard(league, me, datetime.now(), player_news(league, me.roster), week))
        _cache[week] = {"at": time.time(), "data": data, "partial": False}
    except Exception as e:
        logging.exception("Full dashboard build failed for week %s", week)
        if _cache.get(week, {}).get("partial"):
            _cache[week]["failed"] = getattr(e, "detail", str(e))
    finally:
        with _building_lock:
            _building.discard(week)


def start_build(week, fresh=False):
    """Run build_full in the background unless one is already running for that week."""
    with _building_lock:
        if week in _building:
            return
        _building.add(week)
    if BACKGROUND:
        threading.Thread(target=build_full, args=(week, fresh), daemon=True).start()
    else:
        build_full(week, fresh)


def game_lines(p, team_id, lineups, now, first, last):
    """{date: projected line} for p's games not started yet this week; with a team_id, only days p starts."""
    if p.injuryStatus == "OUT":
        return {}
    avg = proj_avg(p)
    line = {k: avg.get(k, 0) for k in streaming.KEYS}
    return {x["date"].date(): line for x in p.schedule.values()
            if now < x["date"] and first <= x["date"].date() <= last
            and (team_id is None or starting(p, team_id, x["date"].date(), lineups))}


def healthy(p):
    return p.injuryStatus in (None, "", "ACTIVE", "NORMAL") and not getattr(p, "injured", False)


SLOT_ORDER = ["PG", "SG", "SF", "PF", "C", "G", "F", "SG/SF", "G/F", "PF/C", "F/C", "UT", "BE", "IR"]


def league_order(roster, today):
    """Sort a roster in place like the league shows it: starters playing today, other starters, healthy bench,
    injured (day-to-day/out) bench, then IR; ESPN's slot order (PG, SG, ..., UT) within each group."""
    def key(p):
        slot = p.lineupSlot
        plays_today = game_dates(p, today, today) and getattr(p, "pending_from", today) <= today  # pending adds join later
        group = 4 if slot == "IR" else (2 if healthy(p) else 3) if slot == "BE" else 0 if plays_today else 1
        return group, SLOT_ORDER.index(slot) if slot in SLOT_ORDER else len(SLOT_ORDER)
    roster.sort(key=key)


def free_agents(league, size=60):
    """Most-owned free agents (not waivers: those can't be added the same day), with their NBA schedule attached."""
    filters = {"players": {"filterStatus": {"value": ["FREEAGENT"]}, "limit": size,
                           "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}}
    data = league.espn_request.league_get(params={"view": "kona_player_info", "scoringPeriodId": league.scoringPeriodId},
                                          headers={"x-fantasy-filter": json.dumps(filters)})
    # espn_api's own free_agents() leaves out the schedule, which the planner needs
    players = []
    for p in data.get("players", []):
        try:
            players.append(Player(p, league.year, league.pro_schedule))
        except Exception:
            logging.exception("Skipped a free agent ESPN sent in an unexpected shape")  # one bad entry shouldn't sink the plan
    return players


def lineup_slots(league):
    """The league's starting slots as ESPN names them (e.g. PG, SG, G, F, C, UT x3), from its roster settings;
    None if ESPN doesn't say (the planner then just counts filled spots). Read once per league object."""
    if not hasattr(league, "_lineup_slots"):
        try:
            counts = league.espn_request.league_get(params={"view": "mSettings"})["settings"]["rosterSettings"]["lineupSlotCounts"]
            slots = [POSITION_MAP[int(k)] for k, n in counts.items() for _ in range(int(n))
                     if POSITION_MAP.get(int(k)) not in (None, "", "BE", "IR")]
            league._lineup_slots = slots or None
        except Exception:
            logging.exception("Couldn't read the league's lineup slots")
            league._lineup_slots = None
    return league._lineup_slots


def acquisitions_left(league, me, first, last, week):
    """Weekly add limit, adds used this matchup and adds left, plus what they were read from so the page can show it.
    Used = your free-agent and waiver adds dated inside the matchup, from ESPN's league activity log."""
    limit = league.settings.matchup_acquisition_limit
    limit = int(limit) if limit and limit > 0 else None  # ESPN may send 1.0; counts must be whole numbers
    # with ESPN's "per scoring period" flag the setting is per day; it counts toward the matchup as limit x days
    per_period = limit if limit and getattr(league.settings, "matchup_limit_per_scoring_period", False) else None
    info = {"limit": per_period * len(days_between(first, last)) if per_period else limit, "per_period": per_period,
            "used": None, "left": None,
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
    lineups, rosters = day_lineups(league, first, last, today, {me.team_id} | ({opp.team_id} if opp else set()), week)
    live = live_days(league, lineups, today)  # same live totals as the dashboard
    my_cats = with_live(my_cats, lineups, live, me.team_id)
    opp_cats = with_live(opp_cats, lineups, live, opp.team_id) if opp else opp_cats
    for team in (me, opp) if opp else (me,):  # pending adds/drops first, then the same roster order on every page
        pending_moves(team, rosters, today, league)
        league_order(team.roster, today)
    keepers =[p for p in me.roster if not hasattr(p, "pending_until")]  # pending drops are gone for planning purposes
    acquisitions = acquisitions_left(league, me, first, last, week)
    out = {"week": {"number": week, "start": first.isoformat(), "end": last.isoformat()},
           "opponent": opp.team_name if opp else None,
           "acquisitions": acquisitions,
           "roster": [{"id": p.playerId, "name": p.name, "team": p.proTeam, "slot": p.lineupSlot,
                       "injury": p.injuryStatus if p.injuryStatus not in (None, "ACTIVE") else None, "pending": getattr(p, "pending", None),
                       "starts_left": len(game_lines(p, me.team_id, lineups, now, first, last))} for p in keepers]}
    if not (plan and opp):
        return out
    roster_ids = {p.playerId for p in keepers}  # a player already dropped can't be dropped again
    droppable = {int(x) for x in drops.split(",") if x.strip().isdigit()} & roster_ids
    # only healthy free agents: OUT, injured reserve, day-to-day or suspended players can't be counted on to play
    fas = [p for p in free_agents(league) if healthy(p)]
    news = player_news(league, me.roster + fas + opp.roster)  # same recipe for both teams and free agents
    project_players(me.roster + fas + opp.roster, news, now, game_logs(me.roster + fas + opp.roster))

    next_first, next_last = week_dates(league, week + 1, today)

    def entry(p, team_id, news_items, name=None):
        """A player for the planner, with the site's projection, his last 3 games and his games through next week."""
        lines = game_lines(p, team_id, lineups, now, first, last)
        season = {k: season_avg(p).get(k, 0) for k in streaming.KEYS}  # what ESPN shows
        ahead = sorted((g for g in p.schedule.values() if today <= g["date"].date() <= next_last), key=lambda g: g["date"])
        return {"id": p.playerId, "name": name or p.name, "note": p.proj_why, "per_game": proj_avg(p),
                "season": season, "factor": p.proj_factor, "basis": p.proj_basis, "recent": p.recent,
                "elig": [x for x in p.eligibleSlots if x not in ("BE", "IR")],  # lineup slots he can fill
                "upcoming": [{"date": g["date"].date().isoformat(), "opp": g["team"]} for g in ahead],
                "games" if team_id else "lines": lines}

    opp_players = [entry(p, opp.team_id, []) for p in opp.roster]
    mine = [entry(p, me.team_id, news.get(p.playerId, [])) for p in me.roster]
    fa_entries = [entry(p, None, news.get(p.playerId, []), f"{p.name} ({p.proTeam})") for p in fas]
    values = streaming.player_values({e["id"]: e["per_game"] for e in mine + fa_entries + opp_players})
    # games in next week's matchup; nothing for anyone OUT now, since his return date is unknown
    next_games = {p.playerId: 0 if p.injuryStatus == "OUT" else
                  sum(next_first <= x["date"].date() <= next_last for x in p.schedule.values()) for p in keepers + fas}
    out["plan"] = streaming.plan(
        days=days_between(max(today, first), last),
        slots=sum(1 for p in me.roster if p.lineupSlot not in ("BE", "IR") and not hasattr(p, "pending_from")),  # today's starting spots
        mine=mine,
        opp_totals=streaming.totals(scores(opp_cats), opp_players),
        current=scores(my_cats),
        free_agents=fa_entries,
        values=values,
        next_games=next_games,
        droppable=droppable,
        acquisitions=acq if acq is not None else (acquisitions["left"] or 0),
        opp_rem=streaming.remaining(opp_players),
        lineup=lineup_slots(league),
    )
    out["plan"]["opp_current"] = scores(opp_cats)
    out["plan"]["opp_players"] = streaming.rows(opp_players)
    for r in out["roster"]:  # overall value next to each name helps decide who is safe to drop
        r["value"] = round(values.get(r["id"], 0), 1)
    return out


@app.get("/api/dashboard")
def dashboard(refresh: bool = False, week: Annotated[int | None, Query(ge=1, le=30)] = None):
    """Saved data right away when there is any; otherwise a quick first version (no news or actual game stats).
    Either way a full build runs in the background when needed, and "pending" tells the page to check back."""
    hit = _cache.get(week)
    # Refresh may skip the cache only after 60s, so repeat clicks can't flood ESPN with your cookie
    if hit and not hit.get("partial") and time.time() - hit["at"] < (60 if refresh else CACHE_SECONDS):
        return hit["data"]
    if hit and hit.get("failed") and week not in _building and not refresh:
        return {**hit["data"], "warning": f"Couldn't load news and actual stats: {hit['failed']}"}
    if not hit:
        league, me = connect()
        hit = _cache[week] = {"at": time.time(), "partial": True,
                              "data": stamped(build_dashboard(league, me, datetime.now(), {}, week, actuals=False))}
    start_build(week, fresh=refresh)
    hit = _cache[week]  # an inline build (tests) may have finished already
    return hit["data"] if not hit.get("partial") and week not in _building else {**hit["data"], "pending": True}


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
                        "MIN": st.get("MIN", ""), "FG%": st.get("FG", ""), "FT%": st.get("FT", ""),
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
    return {"MIN": num(st.get("MIN")), "FGM": fgm, "FGA": fga, "FTM": ftm, "FTA": fta, **{k: num(st.get(k)) for k in COUNTING}}


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


SCOREBOARD_URL = "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={}"
SUMMARY_URL = "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={}"
LIVE_SECONDS = 30
_live = {}  # cache key -> (time, parsed value); small parsed values only


def cached(key, ttl, fetch):
    hit = _live.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = fetch()
    _live[key] = (time.time(), value)
    if len(_live) > 100:  # a day has ~15 games; drop the oldest entries so the cache can't grow without end
        for k in sorted(_live, key=lambda k: _live[k][0])[:50]:
            _live.pop(k, None)
    return value


INJURIES_URL = "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"


def injured_out():
    """Names of every NBA player ESPN lists as Out right now (all teams), cached 10 minutes."""
    return cached("injuries", 600, lambda: {i["athlete"]["displayName"] for t in get_json(INJURIES_URL).get("injuries", [])
                                            for i in t.get("injuries", []) if i.get("status") == "Out"})


def get_json(url):
    with urlopen(Request(url, headers=BROWSER_UA), timeout=10) as r:
        return json.load(r)


def box_line(labels, stats):
    """An ESPN box-score row as the same stat dict the game log uses (FG%/FT% hold makes-attempts)."""
    st = dict(zip(labels, stats))
    return {"MIN": st.get("MIN", ""), "FG%": st.get("FG", ""), "FT%": st.get("FT", ""), "3PM": st.get("3PT", "").split("-")[0],
            **{k: st.get(k, "") for k in ("PTS", "REB", "AST", "STL", "BLK", "TO")}}


def parse_summary(data):
    """{athlete id: stat line so far} from a game summary's box score."""
    lines = {}
    for team in data.get("boxscore", {}).get("players", []):
        for block in team.get("statistics", []):
            for a in block.get("athletes", []):
                if a.get("stats"):
                    lines[str(a["athlete"]["id"])] = box_line(block.get("labels", []), a["stats"])
    return lines


def todays_game(team, day):
    """The NBA team's game on `day` (fantasy team code): status, opponent and every player's box-score line so far."""
    board = cached(f"board:{day}", LIVE_SECONDS, lambda: get_json(SCOREBOARD_URL.format(day.strftime("%Y%m%d"))))
    for ev in board.get("events", []):
        sides = {SITE_TO_FANTASY.get(c["team"]["abbreviation"], c["team"]["abbreviation"]): c.get("homeAway")
                 for c in ev["competitions"][0]["competitors"]}
        if team not in sides:
            continue
        status = ev["status"]["type"]
        game = {"opp": next(t for t in sides if t != team), "at": "vs" if sides[team] == "home" else "@",
                "state": status.get("state"), "status": status.get("shortDetail") or status.get("description"), "players": {}}
        if game["state"] != "pre":  # box score once the game has started
            game["players"] = cached(f"box:{ev['id']}", LIVE_SECONDS, lambda: parse_summary(get_json(SUMMARY_URL.format(ev["id"]))))
        return game
    return None


@app.get("/api/live/{player_id}")
def live(player_id: int):
    """Today's game for one of your players (status + his line so far) and his last 3 finished games."""
    row = next((p for hit in _cache.values() for p in hit["data"].get("roster", []) if p["id"] == player_id), None)
    if not row:  # only your roster, like the head-to-head popup
        raise HTTPException(404, "Player not on your roster")
    today = datetime.now().date()
    out = {"updated": datetime.now(timezone.utc).isoformat(), "today": None, "last3": [], "errors": []}
    try:
        game = todays_game(row["team"], today)
        if game:
            out["today"] = {k: v for k, v in game.items() if k != "players"} | {"line": game["players"].get(str(player_id))}
    except Exception as e:
        out["errors"].append(f"live game: {e}")
    for season in (current_season(), current_season() - 1):  # last season fills in early in the year
        try:
            out["last3"] += [g for g in fetch_gamelog(player_id, season) if g["date"] < today.isoformat()][:3 - len(out["last3"])]
        except Exception as e:
            out["errors"].append(f"game log {season}: {e}")
        if len(out["last3"]) >= 3:
            break
    return out


ACCURACY_HISTORY = 5  # a game is scored once the player has this many earlier games this season


def track_record(log):
    """Replay one player's season: before each game, what the site's model would have projected from his earlier
    games, next to what he actually did. Returns (totals projected, totals actual, games scored, last 10 games)."""
    games = sorted((g for g in log if (g["stats"].get("MIN") or "0") not in ("0", "", "-")), key=lambda g: g["date"])
    lines = [stat_line(g) | {"date": g["date"]} for g in games]
    proj_tot = {k: 0.0 for k in backtest.STATS}
    act_tot = {k: 0.0 for k in backtest.STATS}
    recent = []
    for i in range(ACCURACY_HISTORY, len(lines)):
        proj = backtest.site(lines[:i], when=games[i]["date"])
        for k in backtest.STATS:
            proj_tot[k] += proj[k]
            act_tot[k] += lines[i][k]
        recent.append({"date": games[i]["date"], "opp": games[i]["opp"], "at": games[i]["at"], "result": games[i]["result"],
                       "proj": {k: round(proj[k], 2) for k in backtest.STATS}, "actual": {k: lines[i][k] for k in backtest.STATS}})
    return proj_tot, act_tot, len(recent), recent[-10:][::-1]


@app.get("/api/accuracy")
def accuracy():
    """Projected vs actual, game by game, for your roster this season (rebuilt from game logs, nothing stored)."""
    hit = _cache.get(None) or next(iter(_cache.values()), None)
    if not hit:
        raise HTTPException(409, "Open the dashboard first so the site knows your roster.")
    roster = hit["data"].get("roster", [])

    def one(r):
        try:
            # finished games only: today's game may still be in progress
            log = [g for g in fetch_gamelog(r["id"], current_season()) if g["date"] < datetime.now().date().isoformat()]
            proj, act, n, recent = track_record(log)
        except Exception as e:
            return {"id": r["id"], "name": r["name"], "team": r["team"], "error": str(e), "games": 0}
        return {"id": r["id"], "name": r["name"], "team": r["team"], "games": n,
                "proj": {k: round(v, 1) for k, v in proj.items()}, "actual": act, "recent": recent}

    with ThreadPoolExecutor(8) as pool:
        players = list(pool.map(one, roster))
    scored = [p for p in players if p["games"]]
    team = {"games": sum(p["games"] for p in scored),
            "proj": {k: round(sum(p["proj"][k] for p in scored), 1) for k in backtest.STATS},
            "actual": {k: sum(p["actual"][k] for p in scored) for k in backtest.STATS}}
    return {"players": players, "team": team, "history": ACCURACY_HISTORY}


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
