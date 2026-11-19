"""Self-check with fake ESPN objects (no network). Run: python test_server.py"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace as NS

import server

server.BACKGROUND = False  # run dashboard builds inline so tests see their results
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
server._cache.clear(); server._league["value"] = None
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
assert server.player_news(nl, me.roster) == {1: [], 2: [], 3: []}  # stat-line news is dropped

# only injury / playing-time news is kept, labeled; at most 3
def feed(pid):
    return {"news": {"feed": [
        {"headline": "Johnson scores 30", "story": "He went 12-for-20 in a win."},
        {"headline": "Johnson questionable Friday", "story": "Johnson is <b>questionable</b> with a sprained ankle."},
        {"headline": "Johnson moved to the bench", "story": "Johnson will come off the bench as the team starts Smith."},
        {"headline": "Johnson notches double-double", "story": "He had 14 points and 11 boards. He has started all season."},
        {"headline": "Johnson out", "story": "Johnson will miss Saturday's game with knee soreness."},
        {"headline": "Johnson plays 38 minutes", "story": "Johnson saw 38 minutes as his role expands."},
    ]}}
server._news.clear()  # news is cached per player for 10 minutes
kept = server.player_news(NS(espn_request=NS(get_player_news=feed)), [me.roster[0]])[1]
assert [(n["kind"], n["headline"]) for n in kept] == [("Injury", "Johnson questionable Friday"), ("Playing time", "Johnson moved to the bench"),
                                                     ("Injury", "Johnson out")], kept
assert server.news_kind("Scored 30 in a win. Fourth straight game with 20.") is None
assert server.news_kind("Johnson had 20 points and 9 rebounds in 32 minutes Tuesday.") is None  # minutes in a recap isn't a role change
assert server.news_kind("Johnson is expected to see more minutes with Young sidelined") == "Injury"  # teammate injury still matters
assert server.news_kind("Coach says Johnson will see more minutes going forward") == "Playing time"
assert server.news_kind("Will be held out to rest on the second night of a back-to-back") == "Injury"

# refresh: served from cache when under 60s old, refetched after
import time as _t
server._cache[None] = {"at": _t.time() - 30, "data": {"cached": True}, "partial": False}
assert server.dashboard(refresh=True) == {"cached": True}

# a fresh ESPN pull is stamped with its time; cached responses keep the old stamp
os.environ["LEAGUE_ID"] = "1"
saved = server.League, server.find_my_team, server.player_news, server.build_dashboard
server.League, server.find_my_team, server.player_news, server.build_dashboard = (lambda **kw: None), (lambda *a: NS(roster=[])), (lambda *a: {}), (lambda *a, **k: {})
server._cache.clear(); server._league["value"] = None
stamp = server.dashboard()["updated"]
assert datetime.fromisoformat(stamp).tzinfo is not None and server.dashboard()["updated"] == stamp

# login reused between requests; refresh logs in again
logins = []
server.League = lambda **kw: logins.append(1) or NS()
server._league["value"] = None
server.connect(); server.connect()
assert len(logins) == 1
server.connect(fresh=True)
assert len(logins) == 2
server._league["value"] = None  # don't leave the fake login behind for later tests

# no saved data: a quick first version (no news, no actual stats) comes back with "pending"; the full build replaces it
built = []
server.build_dashboard = lambda league, me, now, news, week=None, actuals=True: built.append(actuals) or {"actuals": actuals}
real_start = server.start_build
server.start_build = lambda week, fresh=False: server._building.add(week)  # a background build that hasn't finished yet
server._cache.clear(); server._building.clear()
first = server.dashboard(week=7)
assert first["actuals"] is False and first["pending"] and built == [False]
server._building.discard(7)
server.build_full(7)  # what the background thread does
assert server.dashboard(week=7)["actuals"] is True and "pending" not in server.dashboard(week=7)
# old saved data is shown right away while a new build runs
server._cache[7]["at"] -= 10_000
stale = server.dashboard(week=7)
assert stale["actuals"] is True and stale["pending"]
# a failed full build: the quick version stays, with a warning, and stops asking the page to poll
server._cache[8] = {"at": 0, "partial": True, "data": {"actuals": False}}
server.build_dashboard = lambda *a, **k: 1 / 0
server.build_full(8)
assert "warning" in server.dashboard(week=8) and "pending" not in server.dashboard(week=8)
server.start_build = real_start
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
assert server.team_games(NS(pro_schedule=pro2), MON, sun) == {"ATL": [0, 1, 0, 0, 0, 0, 0], "BOS": [0, 1, 0, 0, 0, 0, 0]}  # Nov 30 is outside the week
assert set(d["planner"]) == {"mine", "teams", "theirs"}
# impact: started days scored, other days blank; week = sum; opponents per day; two best categories
jj_row = d["planner"]["mine"][0]
assert [x is not None for x in jj_row["impact"]] == [s == "start" for s in jj_row["days"]], jj_row
assert jj_row["impact_week"] == round(sum(x for x in jj_row["impact"] if x is not None), 1)
assert jj_row["opps"][0] == "BOS" and jj_row["opps"][1] is None and len(jj_row["top"]) == 2
# a better line scores higher impact than a worse one against the same pool
zs = __import__("streaming").zscorer({1: {"PTS": 20, "FGM": 8, "FGA": 16}, 2: {"PTS": 5, "FGM": 2, "FGA": 8}})
assert sum(zs({"PTS": 30, "FGM": 12, "FGA": 20}).values()) > sum(zs({"PTS": 4, "FGM": 1, "FGA": 9}).values())
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
server._cache.clear(); server._cache[6] = {"at": 0, "data": {"roster": [{"id": 7}]}, "partial": False}
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
# your league: limit 1 "per scoring period" -> 1 x 7 days = 7 this matchup; 1 add so far -> 6 left
daily_league = NS(settings=NS(matchup_acquisition_limit=1.0, matchup_limit_per_scoring_period=True),  # ESPN sends a float
                  recent_activity=lambda size: [acts[0]], espn_request=NS(league_get=lambda params: teams_raw))
info = server.acquisitions_left(daily_league, me, MON, MON + timedelta(days=6), 5)
assert (info["limit"], info["per_period"], info["used"], info["left"]) == (7, 1, 1, 6), info
assert all(type(info[k]) is int for k in ("limit", "per_period", "used", "left")), info  # range() in the planner needs ints
broken = server.acquisitions_left(NS(settings=NS(matchup_acquisition_limit=-1), recent_activity=lambda size: 1 / 0,
                                     espn_request=NS(league_get=lambda params: {})), me, MON, MON, 5)
assert broken["limit"] is None and broken["left"] is None and len(broken["errors"]) == 2, broken

fa = f(50, "Free Guy", [4, 5, 6], PTS=25)  # Fri-Sun, after NOW
scrub = f(51, "Scrub", [], slot="BE")
me.roster.append(scrub)
saved = server.connect, server.free_agents, server.day_lineups, server.acquisitions_left, server.datetime
league.scoreboard = lambda week: [NS(home_team=me, away_team=opp, home_team_cats=cats, away_team_cats=None)]
server.connect = lambda: (league, me)
hurt_star = f(52, "Hurt Star", [], injury="OUT", PTS=40, REB=12, AST=10)  # great season value, but out
dtd = f(53, "Questionable Guy", [4, 5, 6], injury="DAY_TO_DAY", PTS=30)
flagged = f(54, "Flagged", [4, 5, 6], PTS=30); flagged.injured = True
server.free_agents = lambda lg: [hurt_star, dtd, flagged, fa]
server.day_lineups = lambda *a: {}
server.acquisitions_left = lambda *a: {"limit": 4, "per_period": None, "used": 2, "left": 2, "counted": []}
server.datetime = type("D", (datetime,), {"now": classmethod(lambda c, tz=None: NOW)})
out = server.stream(drops="51,999,abc", plan=True)
assert out["acquisitions"]["left"] == 2 and out["opponent"] == opp.team_name
moves = out["plan"]["moves"]
assert moves and moves[0]["add"]["name"] == "Free Guy (ATL)" and moves[0]["drop"]["name"] == "Scrub", moves
assert all(m["add"]["name"].split(" (")[0] == "Free Guy" for m in moves), moves  # injured / day-to-day free agents never suggested
assert moves[0]["add"]["line"]["PTS"] == 25 and moves[0]["drop"]["line"] is not None  # per-game lines for the side-by-side view
assert moves[0]["add"]["factor"] == 1 and moves[0]["add"]["note"] == ""
assert server.healthy(fa) and not server.healthy(hurt_star) and not server.healthy(dtd) and not server.healthy(flagged)
# breakdown: rows + this week's totals add up to the planned totals; the pickup is labeled
pl = out["plan"]
assert any(r["name"].startswith("Free Guy (ATL) · added") for r in pl["players"]), pl["players"]
assert sum(r["PTS"] for r in pl["players"]) + pl["current"].get("PTS", 0) == pl["after"]["mine"]["PTS"]
assert sum(r["PTS"] for r in pl["opp_players"]) + pl["opp_current"].get("PTS", 0) == pl["theirs"]["PTS"]
assert server.stream(drops="999", plan=True)["plan"]["moves"] == []  # 999 isn't on your roster, so nothing is droppable
assert "plan" not in server.stream()  # roster + acquisitions only until you ask for a plan
server.connect, server.free_agents, server.day_lineups, server.acquisitions_left, server.datetime = saved

# free agents: an entry that can't be read is skipped, the rest still come back
good = {"id": 70, "fullName": "Good FA", "defaultPositionId": 1, "eligibleSlots": [0], "proTeamId": 1, "injuryStatus": "ACTIVE",
        "player": {"id": 70, "fullName": "Good FA", "defaultPositionId": 1, "eligibleSlots": [0], "proTeamId": 1, "stats": []}}
bad = {"id": 71, "player": {}}
fa_league = NS(year=2027, scoringPeriodId=30, pro_schedule={}, espn_request=NS(league_get=lambda params, headers: {"players": [bad, good]}))
assert [p.name for p in server.free_agents(fa_league)] == ["Good FA"]

# unexpected errors come back as JSON with the reason (shown on the page), not a bare 500
resp = server.server_error(NS(url=NS(path="/api/stream")), KeyError("eligibleSlots"))
assert resp.status_code == 500 and b"KeyError" in resp.body
me.roster.remove(scrub)

# role: news direction
assert server.role_direction("Smith will start Friday in place of the injured Jones") == 1
assert server.role_direction("Smith is expected to see more minutes with Jones sidelined") == 1
assert server.role_direction("Smith was moved to the bench and will play a reduced role") == -1
assert server.role_direction("Smith is questionable with an ankle sprain") == 0

# role: recent minutes scale the whole line; the newest role news (last 7 days) moves it again
def rp(season_min, recent_min, gp=4):
    return NS(year=2027, stats={"2027_total": {"avg": {"MIN": season_min, "PTS": 10}},
                                "2027_last_7": {"avg": {"MIN": recent_min, "GP": gp}}})
f1, why1 = server.role_factor(rp(20, 30), [], NOW)
assert round(f1, 2) == 1.5 and "minutes 20.0 → 30.0" in why1[0], (f1, why1)
assert server.role_factor(rp(28.8, 27.6), [], NOW)[1] == ["minutes 28.8 → 27.6 (last 7 days)"]  # small changes are explained too
assert server.role_factor(rp(28.8, 28.5), [], NOW)[1] == []  # under 3%: noise
assert server.role_factor(rp(20, 30, gp=1), [], NOW)[0] == 1.0  # one game isn't a trend
assert server.role_factor(rp(20, 60), [], NOW)[0] == 1.6  # capped
fresh = {"role": 1, "headline": "Smith will start", "published": (NOW - timedelta(days=1)).isoformat() + "Z"}
old = dict(fresh, published=(NOW - timedelta(days=20)).isoformat() + "Z")
assert round(server.role_factor(rp(20, 20), [fresh], NOW)[0], 2) == 1.2
assert server.role_factor(rp(20, 20), [old], NOW)[0] == 1.0  # stale news ignored
assert server.role_factor(rp(20, 20), [dict(fresh, role=-1)], NOW)[0] == 0.75

# the planner passes the reason along with the pickup
pl = __import__("streaming").plan(
    [MON + timedelta(days=4)], 1, [{"id": 1, "name": "Bench", "games": {}}],
    {c: 0 for c in __import__("streaming").CATS}, {}, [{"id": 9, "name": "Riser", "note": "minutes 20 → 30 (last 7 days)",
     "lines": {MON + timedelta(days=4): {"PTS": 30, "REB": 9, "AST": 5, "STL": 1, "BLK": 1, "3PM": 2, "TO": -1, "FGM": 9, "FGA": 15, "FTM": 5, "FTA": 6}}}],
    {1}, 3)
assert pl["moves"][0]["why"] == "minutes 20 → 30 (last 7 days)" and "minutes 20 → 30" in pl["players"][1]["name"], pl
assert pl["moves"][0]["add"]["line"] is None and pl["moves"][0]["add"]["factor"] == 1  # no stat lines given: page shows blanks
server._league["value"] = None; server._cache.clear(); server._news.clear()  # leave no fake state behind
print("ok")
