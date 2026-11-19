"""Self-check with fake ESPN objects (no network). Run: python test_server.py"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace as NS

import server

TODAY = date(2026, 11, 18)  # Wednesday
NOW = datetime(2026, 11, 18, 12)  # noon, before tonight's 7pm games
MON = TODAY - timedelta(days=2)


def player(pid, name, game_days, injury="ACTIVE", slot="UT", **avg):
    sched = {str(i): {"team": "BOS", "date": datetime.combine(MON + timedelta(days=d), datetime.min.time()).replace(hour=19)}
             for i, d in enumerate(game_days)}
    avg = {"FGM": 8, "FGA": 16, "FTM": 4, "FTA": 5, "PTS": 22, "REB": 6, "AST": 5,
           "STL": 1, "BLK": 1, "3PM": 2, "TO": 3, **avg}
    return NS(playerId=pid, name=name, year=2027, schedule=sched, injuryStatus=injury, lineupSlot=slot,
              eligibleSlots=["PG", "SG", "G", "UT"], proTeam="ATL",
              stats={"2027_total": {"avg": avg}})


me = NS(team_id=1, team_name="Neutered", owners=[{"id": "{ABC}"}], roster=[
    player(1, "Jalen Johnson", [0, 2, 4, 6]),
    player(2, "Devin Booker", [1, 3, 5]),
    player(3, "Hurt Guy", [2, 3, 4], injury="OUT"),
])
opp = NS(team_id=2, team_name="Dylan's Dangerous Team", owners=[], roster=[
    player(4, "Opp A", [2, 3, 4, 5, 6], PTS=10, TO=1),
])
cats = {"PTS": {"score": 50}, "REB": {"score": 20}}
league = NS(settings=NS(name="Family n Friends", matchup_periods={str(i): [i] for i in range(1, 21)}), teams=[me, opp], currentMatchupPeriod=5,
            scoreboard=lambda week: [NS(home_team=me, away_team=opp, home_team_cats=cats, away_team_cats=None)] if week == 5 else [])

assert server.find_my_team(league, None, "{abc}") is me
assert server.find_my_team(league, "2", None) is opp

d = server.build_dashboard(league, me, NOW, {1: [{"headline": "h", "story": "s"}]})
assert d["slate"]["mine"] == [1, 1, 1, 1, 1, 1, 1], d["slate"]  # OUT player excluded
assert d["slate"]["theirs"] == [0, 0, 1, 1, 1, 1, 1]
assert [p["games_left"] for p in d["roster"]] == [3, 2, 3]
# Remaining (Wed-Sun): Johnson 3 games + Booker 2 = 5 games x 22 + current 50
assert d["projection"]["mine"]["PTS"] == 5 * 22 + 50
assert d["projection"]["theirs"]["PTS"] == 5 * 10
assert d["projection"]["mine"]["FG%"] == 0.5
r = d["projection"]["result"]
assert r["PTS"] == "W" and r["TO"] == "L" and r["FG%"] == "T", r
assert d["roster"][0]["news"][0]["headline"] == "h"

# only your set starters count: bench and IR players' games are ignored
f = player
assert server.daily_slate([f(1, "a", [3]), f(2, "b", [3], slot="BE"), f(3, "c", [3], slot="IR")], 1, MON, MON + timedelta(days=6), TODAY) == [0, 0, 0, 1, 0, 0, 0]
assert server.project([f(1, "a", [3]), f(2, "b", [3], slot="BE")], None, NOW, MON, MON + timedelta(days=6))["PTS"] == 22

# per-day ESPN lineups: Tuesday (past) counts starters who actually played; later days use that day's lineup
TUE, THU = MON + timedelta(days=1), MON + timedelta(days=3)
a, b, c = f(1, "a", [1, 3], slot="BE"), f(2, "b", [1, 3]), f(3, "c", [1, 3], injury="OUT")
lu = {TUE: {1: {1: True, 2: True, 3: False}}, THU: {1: {1: True, 3: True}}}  # Tue: a & b played, c (OUT) didn't; Thu: a & c start
assert server.daily_slate([a, b, c], 1, MON, MON + timedelta(days=6), TODAY, lu) == [0, 2, 0, 1, 0, 0, 0]  # Thu: c is OUT, b benched
rows = server.contributions([a, b, c], NOW, MON, MON + timedelta(days=6), 1, lu)
assert [(r["name"], r["games"]) for r in rows] == [("a", 1)], rows  # only a starts Thursday and is available

# played(): that day's real stats, named; projections or empty stat lines don't count
e = lambda *s: {"playerPoolEntry": {"player": {"stats": list(s)}}}
day = server.played(e({"scoringPeriodId": 30, "statSourceId": 0, "stats": {"0": 12.0, "6": 7.0, "13": 5.0, "14": 9.0, "99": 1}}), 30)
assert day["PTS"] == 12 and day["REB"] == 7 and day["FGM"] == 5 and day["FGA"] == 9 and day["AST"] == 0 and "99" not in day, day
assert not server.played(e({"scoringPeriodId": 30, "statSourceId": 1, "stats": {"0": 12.0}}), 30)
assert not server.played(e({"scoringPeriodId": 30, "statSourceId": 0, "stats": {"0": 0}}), 30)
assert not server.played(e({"scoringPeriodId": 29, "statSourceId": 0, "stats": {"0": 12.0}}), 30)

# day_lineups(): bench/IR dropped, period offset from today, teams without lineup data left out
def fake_get(params, headers):
    p = params["scoringPeriodId"]
    entry = lambda pid, slot: {"playerId": pid, "lineupSlotId": slot, "playerPoolEntry": {"player": {"stats": [{"scoringPeriodId": p, "statSourceId": 0, "stats": {"0": 5}}]}}}
    return {"schedule": [{"home": {"teamId": 1, "rosterForCurrentScoringPeriod": {"entries": [entry(p, 0), entry(99, 12), entry(98, 13)]}}, "away": {"teamId": 2}}]}
lg = NS(scoringPeriodId=30, currentMatchupPeriod=5, espn_request=NS(league_get=fake_get))
got = server.day_lineups(lg, MON, MON + timedelta(days=6), TODAY, {1, 2}, 5)
assert set(got[TODAY][1]) == {30} and set(got[MON][1]) == {28} and got[MON][1][28]["PTS"] == 5, got  # today = period 30, Monday = 28
pro = {1: {"5": [{"date": datetime(2026, 11, 16, 19).timestamp() * 1000, "homeProTeamId": 1, "awayProTeamId": 2}]},
       2: {"5": [{"date": datetime(2026, 11, 16, 19).timestamp() * 1000, "homeProTeamId": 1, "awayProTeamId": 2}]}}
assert server.nba_games(NS(pro_schedule=pro), MON, MON + timedelta(days=6)) == [1, 0, 0, 0, 0, 0, 0]  # same game listed under both teams counts once

# breakdown rows add up to the projection
rows = d["projection"]["players"]["mine"]
assert [(r["name"], r["games"]) for r in rows] == [("Jalen Johnson", 3), ("Devin Booker", 2)]  # OUT player excluded
assert sum(r["PTS"] for r in rows) + d["projection"]["current"]["mine"]["PTS"] == d["projection"]["mine"]["PTS"]

# IR slot never counts, even when only day-to-day
assert not server.starting(player(8, "IR guy", [3], injury="DAY_TO_DAY", slot="IR"), 1, THU, None)
assert server.starting(player(8, "DTD guy", [3], injury="DAY_TO_DAY"), 1, THU, None)
assert not server.starting(player(8, "Bench guy", [3], slot="BE"), 1, THU, None)

# ESPN login failure -> clear 502 message instead of a raw 500
import os
from fastapi import HTTPException
os.environ["LEAGUE_ID"] = "1"
real_league, server.League = server.League, lambda **kw: (_ for _ in ()).throw(Exception("espn_s2 and swid are required"))
server._cache.clear()
try:
    server.dashboard(refresh=True); assert False
except HTTPException as e:
    assert e.status_code == 502 and "ESPN_S2" in e.detail, e.detail
server.League = real_league

# news: one player's failure doesn't drop the others; HTML cleaned
def fake_news(pid):
    if pid == 2:
        raise OSError("timeout")
    return {"news": {"feed": [{"headline": "<b>Hot</b>", "story": "Scored 30. Then more."}] * 5}}
nl = NS(espn_request=NS(get_player_news=fake_news))
assert server.player_news(nl, me.roster) == {1: [{"headline": "Hot", "tldr": "Scored 30."}] * 3, 2: [], 3: [{"headline": "Hot", "tldr": "Scored 30."}] * 3}

# refresh: served from cache when under 60s old, refetched after
import time as _t
server._cache[None] = {"at": _t.time() - 30, "data": {"cached": True}}
assert server.dashboard(refresh=True) == {"cached": True}

# a fresh ESPN pull is stamped with its time; cached responses keep the old stamp
os.environ["LEAGUE_ID"] = "1"
saved = server.League, server.find_my_team, server.player_news, server.build_dashboard
server.League, server.find_my_team, server.player_news, server.build_dashboard = (lambda **kw: None), (lambda *a: NS(roster=[])), (lambda *a: {}), (lambda *a: {})
server._cache.clear()
stamp = server.dashboard()["updated"]
assert datetime.fromisoformat(stamp).tzinfo is not None and server.dashboard()["updated"] == stamp
server.League, server.find_my_team, server.player_news, server.build_dashboard = saved

# after tonight's games: Wednesday is already in the current totals, so it is not projected again
late = server.project(me.roster, cats, datetime(2026, 11, 18, 23), MON, MON + timedelta(days=6))
assert late["PTS"] == 4 * 22 + 50, late["PTS"]  # Johnson Fri+Sun, Booker Thu+Sat; Wed not counted again
# week picker: another week moves the dates, uses that week's matchup, and projects only games inside it
nxt = server.build_dashboard(league, me, NOW, {}, week=6)
assert nxt["week"]["start"] == "2026-11-23" and nxt["week"]["number"] == 6 and nxt["week"]["count"] == 20
assert nxt["opponent"] is None  # fake scoreboard has no week-6 matchup
past = server.build_dashboard(league, me, NOW, {}, week=4)
assert past["week"]["start"] == "2026-11-09" and all(p["games_left"] == 0 and p["upcoming"] == [] for p in past["roster"])
# a future week projects every game in that week, none from this week
nx = f(30, "nx", [3, 8, 9])  # Thu this week, Tue+Wed next week
assert [r["games"] for r in server.contributions([nx], NOW, MON + timedelta(days=7), MON + timedelta(days=13))] == [2]

# real ESPN week dates: matchup period -> scoring periods (days); today (Wed Nov 18) is scoring period 30
espn_weeks = {"1": list(range(1, 14)), "5": list(range(28, 35)), "6": list(range(35, 49))}  # week 6 is a 14-day (All-Star) week
real = NS(settings=NS(name="L", matchup_periods=espn_weeks), scoringPeriodId=30, currentMatchupPeriod=5,
          teams=[me, opp], scoreboard=lambda week: [])
assert server.week_dates(real, 5, TODAY) == (date(2026, 11, 16), date(2026, 11, 22))
assert server.week_dates(real, 1, TODAY) == (date(2026, 10, 20), date(2026, 11, 1))  # opening week: Tue to Sunday after next
assert server.week_dates(real, 6, TODAY) == (date(2026, 11, 23), date(2026, 12, 6))
assert server.week_dates(real, 7, TODAY) == (date(2026, 11, 30), date(2026, 12, 6))  # not in ESPN's map: Mon-Sun fallback
long = server.build_dashboard(real, me, NOW, {}, week=6)
assert (long["week"]["start"], long["week"]["end"], len(long["slate"]["mine"])) == ("2026-11-23", "2026-12-06", 14)
assert long["week"]["count"] == 6  # highest week in ESPN's map

# weekly planner: per-day status follows the slate rules
sun = MON + timedelta(days=6)
assert [server.day_state(x, 1, THU, TODAY, lu) for x in (a, b, c)] == ["start", "bench", "out"]  # Thu lineup: a & c start, c OUT
assert [server.day_state(x, 1, TUE, TODAY, lu) for x in (a, b, c)] == ["start", "start", "dnp"]  # Tue: c started, no stats
assert server.day_state(a, 1, MON, TODAY, lu) == "off"  # no game Monday
rows = server.planner_rows([a], 1, MON, sun, TODAY, lu)
assert rows[0]["days"] == ["off", "start", "off", "start", "off", "off", "off"] and rows[0]["avg"]["PTS"] == 22
pro2 = {1: {"x": [{"date": datetime(2026, 11, 17, 19).timestamp() * 1000}, {"date": datetime(2026, 11, 30, 19).timestamp() * 1000}]},
        2: {"x": [{"date": datetime(2026, 11, 17, 19).timestamp() * 1000}]}}
assert server.team_games(NS(pro_schedule=pro2), MON, sun) == {"ATL": 1, "BOS": 1}  # Nov 30 is outside the week
assert set(d["planner"]) == {"mine", "teams", "theirs"}
# live category leaders from ESPN's current scores (cats fixture: you PTS 50 / REB 20, opponent nothing yet)
live = d["projection"]["current"]["result"]
assert live["PTS"] == "W" and live["REB"] == "W" and live["AST"] == "T", live
# actual stats only for past days a player started and played; None elsewhere
lu2 = {TUE: {1: {1: {"PTS": 31}}}, THU: {1: {1: {"PTS": 99}}}}  # Thursday is not past yet
assert server.planner_rows([a], 1, MON, sun, TODAY, lu2)[0]["actual"] == [None, {"PTS": 31}, None, None, None, None, None]

# bye week: no matchup
league.scoreboard = lambda week: []
assert server.build_dashboard(league, me, NOW, {})["opponent"] is None
# upcoming games: only today..Sunday of the current matchup week
assert len(server.upcoming_games(player(9, "x", [1, 3, 9]), TODAY, MON + timedelta(days=6))) == 1
assert [g["date"] for g in d["roster"][0]["upcoming"]] == ["2026-11-18", "2026-11-20", "2026-11-22"]
assert d["roster"][0]["upcoming"][0]["opp"] == "BOS"

# news TLDR: tags and entities stripped, first sentence only, long text capped
raw = "<p>Johnson&nbsp;scored <b>30</b> points vs. Miami.</p> He also had 12 boards. Coach said more."
assert server.tldr(raw) == "Johnson scored 30 points vs. Miami.", server.tldr(raw)
assert server.tldr(None) == ""
long = server.tldr("word " * 100)
assert len(long) <= 181 and long.endswith("…")
# head-to-head: only regular season + postseason, site team codes mapped, newest first
def ev(eid, opp, day):
    return eid, {"opponent": {"abbreviation": opp}, "gameDate": f"2026-{day}T23:00:00.000+00:00", "atVs": "@", "gameResult": "W", "score": "110-100"}
labels = ["MIN", "FG", "FG%", "3PT", "3P%", "FT", "FT%", "REB", "AST", "BLK", "STL", "PF", "TO", "PTS"]
row = lambda eid: {"eventId": eid, "stats": ["30", "7-15", "46.7", "2-5", "40.0", "5-5", "100.0", "8", "6", "1", "0", "2", "3", "21"]}
log = {"labels": labels, "events": dict([ev("1", "NY", "03-01"), ev("2", "NY", "04-20"), ev("3", "NY", "10-05"), ev("4", "OKC", "03-02"), ev("5", "NY", "02-14")]),
       "seasonTypes": [
           {"displayName": "2025-26 Postseason", "categories": [{"events": [row("2")]}]},
           {"displayName": "2025-26 Regular Season", "categories": [{"events": [row("1"), row("4")]}]},
           {"displayName": "2025-26 Preseason", "categories": [{"events": [row("3")]}]},
           {"displayName": "2025-26 All-Star", "categories": [{"events": [row("5")]}]},
       ]}
g = [x for x in server.parse_gamelog(log) if x["opp"] == "NYK"]
assert [x["date"] for x in g] == ["2026-04-20", "2026-03-01"], g
assert g[0]["stats"] == {"FG%": "7-15", "FT%": "5-5", "3PM": "2", "PTS": "21", "REB": "8", "AST": "6", "STL": "0", "BLK": "1", "TO": "3"}

# vs endpoint: only roster players, cached parsed games, newest 3, bad team rejected
from fastapi import HTTPException
server._cache.clear(); server._cache[6] = {"at": 0, "data": {"roster": [{"id": 7}]}}
fake = {2027: [{"opp": "NYK", "date": "2026-11-01"}], 2026: Exception, 2025: [{"opp": "NYK", "date": f"2025-0{m}-01"} for m in (3, 2, 1)] + [{"opp": "OKC", "date": "2025-04-01"}]}
def fake_fetch(pid, season):
    if fake.get(season) is Exception or season not in fake:
        raise OSError("404")
    return fake[season]
real_fetch, server.fetch_gamelog = server.fetch_gamelog, fake_fetch
assert [g["date"] for g in server.vs_team(7, "NYK")["games"]] == ["2026-11-01", "2025-03-01", "2025-02-01"]
for args, code in (((7, "XXX"), 400), ((999, "NYK"), 404)):
    try:
        server.vs_team(*args); assert False
    except HTTPException as e:
        assert e.status_code == code
# ESPN unreachable even for this season -> 502 with the reason, not an empty "no games"
server.fetch_gamelog = lambda pid, season: (_ for _ in ()).throw(OSError("HTTP Error 403: Forbidden"))
try:
    server.vs_team(7, "NYK"); assert False
except HTTPException as e:
    assert e.status_code == 502 and "403" in e.detail, e.detail

# actual stats from the game log fill the daily lineups up to today (not future days)
log = [{"date": "2026-11-18", "opp": "OKC", "stats": {"FG%": "6-14", "FT%": "3-4", "3PM": "1", "PTS": "16", "REB": "9", "AST": "7", "STL": "1", "BLK": "0", "TO": "2"}},
       {"date": "2026-11-20", "opp": "MIA", "stats": {"FG%": "9-9", "FT%": "0-0", "3PM": "0", "PTS": "18", "REB": "1", "AST": "1", "STL": "0", "BLK": "0", "TO": "0"}}]
server.fetch_gamelog = lambda pid, season: log if pid == 1 else []
lu3 = {TODAY: {1: {1: None, 2: None}}, TODAY + timedelta(days=2): {1: {1: None}}}
server.fill_actuals(lu3, TODAY)
assert lu3[TODAY][1][1] == {"FGM": 6, "FGA": 14, "FTM": 3, "FTA": 4, "PTS": 16, "REB": 9, "AST": 7, "STL": 1, "BLK": 0, "3PM": 1, "TO": 2}, lu3
assert lu3[TODAY][1][2] is None and lu3[TODAY + timedelta(days=2)][1][1] is None  # no log for player 2; Friday is in the future
assert server.planner_rows([f(1, "a", [2])], 1, MON, MON + timedelta(days=6), TODAY, lu3)[0]["actual"][2]["PTS"] == 16  # today's finished game shows
server.fetch_gamelog = real_fetch

# streaming endpoint: acquisitions left from ESPN, droppable ids validated, a free agent fills an open slot
# acquisitions used = your FA/waiver adds dated inside the matchup, from the activity log
ms = lambda d: datetime.combine(d, datetime.min.time()).replace(hour=15).timestamp() * 1000
acts = [NS(date=ms(TODAY), actions=[(me, "FA ADDED", "x"), (me, "DROPPED", "y")]),
        NS(date=ms(MON), actions=[(me, "WAIVER ADDED", "z")]),
        NS(date=ms(MON - timedelta(days=1)), actions=[(me, "FA ADDED", "last week")]),
        NS(date=ms(TODAY), actions=[(opp, "FA ADDED", "theirs")])]
teams_raw = {"teams": [{"id": 1, "transactionCounter": {"matchupAcquisitionTotals": {"5": 1}, "acquisitions": 9}}]}
acq_league = NS(settings=NS(matchup_acquisition_limit=7), recent_activity=lambda size: acts, espn_request=NS(league_get=lambda params: teams_raw))
info = server.acquisitions_left(acq_league, me, MON, MON + timedelta(days=6), 5)
assert (info["limit"], info["used"], info["left"]) == (7, 2, 5), info
assert [c["player"] for c in info["counted"]] == ["x", "z"] and len(info["recent"]) == 3  # recent also has last week's add
assert info["counter"] == {"this_week": 1, "by_week": {"5": 1}, "season": 9} and info["errors"] == []
# your league: limit 1 "per scoring period" = 1 add a day -> 7 a week; 1 add so far -> 6 left
daily_league = NS(settings=NS(matchup_acquisition_limit=1, matchup_limit_per_scoring_period=True),
                  recent_activity=lambda size: [acts[0]], espn_request=NS(league_get=lambda params: teams_raw))
info = server.acquisitions_left(daily_league, me, MON, MON + timedelta(days=6), 5)
assert (info["limit"], info["daily_limit"], info["used"], info["left"]) == (7, 1, 1, 6), info
broken = server.acquisitions_left(NS(settings=NS(matchup_acquisition_limit=-1), recent_activity=lambda size: 1 / 0,
                                     espn_request=NS(league_get=lambda params: {})), me, MON, MON, 5)
assert broken["limit"] is None and broken["left"] is None and len(broken["errors"]) == 2, broken

fa = f(50, "Free Guy", [4, 5, 6], PTS=25)  # Fri-Sun, after NOW
scrub = f(51, "Scrub", [], slot="BE")
me.roster.append(scrub)
saved = server.connect, server.free_agents, server.day_lineups, server.acquisitions_left, server.datetime
league.scoreboard = lambda week: [NS(home_team=me, away_team=opp, home_team_cats=cats, away_team_cats=None)]
server.connect = lambda: (league, me)
server.free_agents = lambda lg: [fa]
server.day_lineups = lambda *a: {}
server.acquisitions_left = lambda *a: {"limit": 4, "daily_limit": None, "used": 2, "left": 2, "counted": []}
server.datetime = type("D", (datetime,), {"now": classmethod(lambda c, tz=None: NOW)})
out = server.stream(drops="51,999,abc", plan=True)
assert out["acquisitions"]["left"] == 2 and out["opponent"] == opp.team_name
moves = out["plan"]["moves"]
assert moves and moves[0]["add"]["name"] == "Free Guy (ATL)" and moves[0]["drop"]["name"] == "Scrub", moves
# breakdown: rows + this week's totals add up to the planned totals; the pickup is labeled
pl = out["plan"]
assert any(r["name"].startswith("Free Guy (ATL) · added") for r in pl["players"]), pl["players"]
assert sum(r["PTS"] for r in pl["players"]) + pl["current"].get("PTS", 0) == pl["after"]["mine"]["PTS"]
assert sum(r["PTS"] for r in pl["opp_players"]) + pl["opp_current"].get("PTS", 0) == pl["theirs"]["PTS"]
assert server.stream(drops="999", plan=True)["plan"]["moves"] == []  # 999 isn't on your roster, so nothing is droppable
assert "plan" not in server.stream()  # roster + acquisitions only until you ask for a plan
server.connect, server.free_agents, server.day_lineups, server.acquisitions_left, server.datetime = saved
me.roster.remove(scrub)
print("ok")
