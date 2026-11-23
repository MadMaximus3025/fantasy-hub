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
assert [p["games_left"] for p in d["roster"]] == [3, 3, 2]  # league order: Hurt Guy plays today (Wed), Booker does not
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
got, got_rosters = server.day_lineups(lg, MON, MON + timedelta(days=6), TODAY, {1, 2}, 5)
assert set(got_rosters[TODAY][1]) == {30, 99, 98}  # full roster incl. bench and IR, for pending adds/drops
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

server._live["injuries"] = (1e12, {"Kevin Durant"})  # ESPN's injury list (Out), pre-filled so tests stay offline
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
# a start filling in for a teammate who's still OUT keeps counting past the named day and the 7-day window
server._news.clear()
sub = lambda pid: {"news": {"feed": [{"headline": "Johnson will start Wednesday", "published": "2026-10-01T15:00:00Z",
                                       "story": "Johnson will start in place of Kevin Durant (ankle)."}]}}
item = server.player_news(NS(espn_request=NS(get_player_news=sub)), [me.roster[0]])[1][0]
assert item["while_out"] == "Kevin Durant" and item["until"], item
fac, why = server.role_factor(None, [item], datetime(2026, 11, 20))
assert fac == 1.2 and "Kevin Durant still out" in why[0], why
server._live["injuries"] = (1e12, set())  # Durant is back: the one-game news expires again
server._news.clear()
item = server.player_news(NS(espn_request=NS(get_player_news=sub)), [me.roster[0]])[1][0]
assert item["while_out"] is None and server.role_factor(None, [item], datetime(2026, 11, 20))[0] == 1.0
server._news.clear()
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
# pending moves: added after games started -> on the roster from tomorrow; dropped -> counts through today only
keep, gone = f(1, "keep", [2, 3]), f(2, "gone", [2, 3, 4])
team = NS(team_id=1, roster=[keep, gone])
new_entry = {"playerId": 70, "lineupSlotId": 0, "playerPoolEntry": {"player": {"id": 70, "fullName": "New Guy", "defaultPositionId": 1,
             "eligibleSlots": [0], "proTeamId": 1, "injuryStatus": "ACTIVE", "stats": []}}}
THU, FRI = TODAY + timedelta(days=1), TODAY + timedelta(days=2)
rost = {TODAY: {1: {1: {}, 2: {}}}, THU: {1: {1: {}, 70: new_entry}}, FRI: {1: {1: {}, 70: new_entry}}}
pro70 = {1: {"x": [{"date": datetime.combine(d, datetime.min.time()).replace(hour=19).timestamp() * 1000, "homeProTeamId": 1, "awayProTeamId": 2}]
             for d in (TODAY, THU)}}
server.pending_moves(team, rost, TODAY, NS(year=2027, pro_schedule={1: {str(i): [g] for i, g in enumerate(
    [{"date": datetime.combine(d, datetime.min.time()).replace(hour=19).timestamp() * 1000, "homeProTeamId": 1, "awayProTeamId": 2} for d in (TODAY, THU)])}}))
added = next(x for x in team.roster if x.playerId == 70)
assert (added.pending, added.pending_from) == ("Added · from Thu", THU) and gone.pending == "Dropped after today" and not hasattr(keep, "pending")
assert server.day_state(added, 1, TODAY, TODAY, None) == "off" and server.day_state(gone, 1, THU, TODAY, None) == "off"
server.pending_moves(team, rost, TODAY, NS(year=2027, pro_schedule={}))
assert len(team.roster) == 3  # running again doesn't duplicate him
server.league_order(team.roster, TODAY)
assert [x.name for x in team.roster][0] == "keep"  # New Guy has a game today but isn't on the team until Thursday

# roster order: starters with a game today, other starters, healthy bench, injured bench, IR; slot order inside
ro = [f(1, "ir", [2], slot="IR"), f(2, "be-out", [2], injury="OUT", slot="BE"), f(3, "be", [2], slot="BE"),
      f(4, "c-off", [3], slot="C"), f(5, "pg-today", [2], slot="PG"), f(6, "ut-today", [2], slot="UT"), f(7, "c-today", [2], slot="C"),
      f(8, "be-dtd", [], injury="DAY_TO_DAY", slot="BE")]
server.league_order(ro, TODAY)
assert [x.name for x in ro] == ["pg-today", "c-today", "ut-today", "c-off", "be", "be-out", "be-dtd", "ir"], [x.name for x in ro]
assert [r["name"] for r in d["roster"]] == ["Jalen Johnson", "Hurt Guy", "Devin Booker"]  # Booker has no game today (Wed)
# availability: OUT / day-to-day players with starts left, their remaining line, and whether they're counted now
av = server.availability([f(1, "a", [3, 5]), f(2, "b", [4], injury="OUT"), f(3, "c", [5], injury="DAY_TO_DAY"),
                          f(4, "d", [], injury="OUT"), f(5, "e", [4], injury="OUT", slot="BE")], NOW, MON, MON + timedelta(days=6), 1, None)
assert [(r["name"], r["status"], r["games"], r["counted"], r["line"]["PTS"]) for r in av] == [
    ("b", "OUT", 1, False, 22), ("c", "DAY_TO_DAY", 1, True, 22)], av  # healthy, no games, and bench players left out
assert set(d["projection"]["availability"]) == {"mine", "theirs"}
# impact: started days scored, other days blank; week = sum; opponents per day; two best categories
jj_row = d["planner"]["mine"][0]
assert [x is not None for x in jj_row["impact"]] == [s == "start" for s in jj_row["days"]], jj_row
assert jj_row["impact_week"] == round(sum(x for x in jj_row["impact"] if x is not None), 1)
assert all((c is None) == (x is None) for c, x in zip(jj_row["impact_cats"], jj_row["impact"]))
assert all(abs(sum(c.values()) - x) < 0.1 for c, x in zip(jj_row["impact_cats"], jj_row["impact"]) if c)
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
assert g[0]["stats"] == {"MIN": "30", "FG%": "7-15", "FT%": "5-5", "3PM": "2", "PTS": "21", "REB": "8", "AST": "6", "STL": "0", "BLK": "1", "TO": "3"}

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
assert lu3[TODAY][1][1] == {"MIN": 0, "FGM": 6, "FGA": 14, "FTM": 3, "FTA": 4, "PTS": 16, "REB": 9, "AST": 7, "STL": 1, "BLK": 0, "3PM": 1, "TO": 2}, lu3
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
server.day_lineups = lambda *a: ({}, {})
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
# a pending drop: not listed, never a drop candidate, and no starting spot added by a pending add
scrub.pending_until, scrub.pending = TODAY, "Dropped after today"
out2 = server.stream(drops="51", plan=True)
assert 51 not in [r["id"] for r in out2["roster"]] and out2["plan"]["moves"] == [], out2["plan"]["moves"]
del scrub.pending_until, scrub.pending
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

# role: only the newest role news (last 7 days) moves the projection; recent minutes don't
def rp(season_min, recent_min, gp=4):
    return NS(year=2027, stats={"2027_total": {"avg": {"MIN": season_min, "PTS": 10}},
                                "2027_last_7": {"avg": {"MIN": recent_min, "GP": gp}}})
f1, why1 = server.role_factor(rp(20, 30), [], NOW)
assert f1 == 1.0 and why1 == [], (f1, why1)  # 20 -> 30 recent minutes: no change
assert server.role_factor(rp(28.8, 28.5), [], NOW)[1] == []  # under 3%: noise
fresh = {"role": 1, "headline": "Smith will start", "published": (NOW - timedelta(days=1)).isoformat() + "Z"}
old = dict(fresh, published=(NOW - timedelta(days=20)).isoformat() + "Z")
assert round(server.role_factor(rp(20, 20), [fresh], NOW)[0], 2) == 1.2
assert server.role_factor(rp(20, 20), [old], NOW)[0] == 1.0  # stale news ignored
assert server.role_factor(rp(20, 20), [dict(fresh, role=-1)], NOW)[0] == 0.75
# one-game news expires once that day has passed; general role news doesn't
pub = (NOW - timedelta(days=3)).isoformat() + "Z"
day3 = server.local_time(pub).date()
assert server.role_until("Recap. Smith will start Wednesday against the Jazz.", pub) == day3 + timedelta(days=(2 - day3.weekday()) % 7)
assert server.role_until("Smith will start tonight.", pub) == day3
assert server.role_until("Smith moves into the starting lineup.", pub) is None
one_game = dict(fresh, published=pub, until=str(day3))  # e.g. "will start tonight", 3 days ago
assert server.role_factor(rp(20, 20), [one_game], NOW)[0] == 1.0
assert server.role_factor(rp(20, 20), [dict(one_game, until="")], NOW)[0] == 1.2  # no day named: still counts

# the planner passes the reason along with the pickup
pl = __import__("streaming").plan(
    [MON + timedelta(days=4)], 1, [{"id": 1, "name": "Bench", "games": {}}],
    {c: 0 for c in __import__("streaming").CATS}, {}, [{"id": 9, "name": "Riser", "note": "minutes 20 → 30 (last 7 days)",
     "lines": {MON + timedelta(days=4): {"PTS": 30, "REB": 9, "AST": 5, "STL": 1, "BLK": 1, "3PM": 2, "TO": -1, "FGM": 9, "FGA": 15, "FTM": 5, "FTA": 6}}}],
    {1}, 3)
assert pl["moves"][0]["why"] == "minutes 20 → 30 (last 7 days)" and "minutes 20 → 30" in pl["players"][1]["name"], pl
assert pl["moves"][0]["add"]["line"] is None and pl["moves"][0]["add"]["factor"] == 1  # no stat lines given: page shows blanks
# live game: scoreboard finds the team's game (site codes mapped), box score gives the player's line so far
board = {"events": [{"id": "9", "status": {"type": {"state": "in", "shortDetail": "5:00 - 2nd"}},
                     "competitions": [{"competitors": [{"team": {"abbreviation": "NY"}, "homeAway": "away"},
                                                       {"team": {"abbreviation": "MIN"}, "homeAway": "home"}]}]}]}
summary = {"boxscore": {"players": [{"statistics": [{"labels": ["MIN", "PTS", "FG", "3PT", "FT", "REB", "AST", "TO", "STL", "BLK"],
           "athletes": [{"athlete": {"id": "77"}, "stats": ["20", "18", "7-12", "2-4", "2-2", "9", "3", "1", "1", "2"]},
                        {"athlete": {"id": "78"}, "stats": []}]}]}]}}
real_get, real_log = server.get_json, server.fetch_gamelog
server.get_json = lambda url: board if "scoreboard" in url else summary
server._live.clear()
g = server.todays_game("NYK", TODAY)
assert (g["opp"], g["at"], g["status"]) == ("MIN", "@", "5:00 - 2nd")
assert g["players"]["77"] == {"MIN": "20", "FG%": "7-12", "FT%": "2-2", "3PM": "2", "PTS": "18", "REB": "9", "AST": "3", "STL": "1", "BLK": "2", "TO": "1"}
assert "78" not in g["players"] and server.todays_game("BOS", TODAY) is None  # no stats yet / no game today
# /api/live: roster only; today's line + last 3 finished games, newest first, today's own log entry left out
server._cache[None] = {"at": _t.time(), "partial": False, "data": {"roster": [{"id": 77, "team": "NYK"}]}}
log77 = [{"date": TODAY.isoformat(), "stats": {}}] + [{"date": f"2026-11-1{i}", "stats": {}} for i in (7, 5, 3, 1)]
server.fetch_gamelog = lambda pid, season: log77
real_dt = server.datetime
server.datetime = type("D", (datetime,), {"now": classmethod(lambda c, tz=None: NOW if tz is None else real_dt.now(tz))})
r = server.live(77)
server.datetime = real_dt
assert r["today"]["line"]["PTS"] == "18" and [x["date"] for x in r["last3"]] == ["2026-11-17", "2026-11-15", "2026-11-13"], r
try:
    server.live(999); assert False
except HTTPException as e:
    assert e.status_code == 404
server.get_json, server.fetch_gamelog = real_get, real_log
server._live.clear()
# accuracy page: replay a season; each game projected from earlier games only; DNPs skipped
glog = [{"date": f"2026-11-{d:02d}", "opp": "BOS", "at": "vs", "result": "W",
         "stats": {"MIN": str(mn), "FG%": "5-10", "FT%": "2-2", "3PM": "1", "PTS": str(pts), "REB": "5", "AST": "3", "STL": "1", "BLK": "0", "TO": "2"}}
        for d, mn, pts in [(1, 30, 10), (2, 30, 10), (3, 30, 10), (4, 30, 10), (5, 30, 10), (6, 30, 20), (7, 0, 0), (8, 30, 30)]]
proj_t, act_t, n, recent = server.track_record(glog)
assert n == 2 and act_t["PTS"] == 50 and abs(proj_t["PTS"] - (10 + 70 / 6)) < 1e-9, (n, act_t["PTS"], proj_t["PTS"])  # 7th (0 min) skipped
assert recent[0]["date"] == "2026-11-08" and recent[0]["proj"]["PTS"] == round(70 / 6, 2) and recent[0]["actual"]["PTS"] == 30
server._cache.clear()
try:
    server.accuracy(); assert False
except HTTPException as e:
    assert e.status_code == 409  # needs the roster from a dashboard load
server._cache[None] = {"at": _t.time(), "partial": False, "data": {"roster": [{"id": 1, "name": "a", "team": "ATL"}, {"id": 2, "name": "b", "team": "ATL"}]}}
real_log = server.fetch_gamelog
server.fetch_gamelog = lambda pid, season: glog if pid == 1 else (_ for _ in ()).throw(OSError("404"))
real_dt = server.datetime
server.datetime = type("D", (datetime,), {"now": classmethod(lambda c, tz=None: datetime(2026, 11, 20, 12) if tz is None else real_dt.now(tz))})
acc = server.accuracy()
server.datetime = real_dt
assert acc["team"]["games"] == 2 and acc["team"]["actual"]["PTS"] == 50 and acc["players"][1]["games"] == 0 and "error" in acc["players"][1]
server.fetch_gamelog = real_log
# one projection everywhere: season average x role factor, stored on the player, used by every page's numbers
riser = f(80, "Riser", [3], PTS=20, MIN=20)
riser.stats["2027_last_7"] = {"avg": {"MIN": 30, "GP": 3}}
server.project_players([riser], {}, NOW)
server.project_players([riser], {80: [{"role": 1, "headline": "Riser will start", "published": (NOW - timedelta(days=1)).isoformat() + "Z"}]}, NOW)
assert riser.proj_factor == 1.2 and riser.proj["PTS"] == 24 and riser.proj_why == "news: Riser will start"
assert server.contributions([riser], NOW, MON, MON + timedelta(days=6))[0]["PTS"] == 24          # dashboard projection
assert server.planner_rows([riser], 1, MON, MON + timedelta(days=6), TODAY, None)[0]["avg"]["PTS"] == 24  # planner
assert server.planner_rows([riser], 1, MON, MON + timedelta(days=6), TODAY, None)[0]["season"]["PTS"] == 20
hurt = f(81, "Hurt", [3], injury="OUT", PTS=20, MIN=20); hurt.stats["2027_last_7"] = {"avg": {"MIN": 30, "GP": 3}}
server.project_players([hurt], {81: [{"role": -1, "headline": "Hurt to the bench", "published": (NOW - timedelta(days=1)).isoformat() + "Z"}]}, NOW)
assert server.availability([hurt], NOW, MON, MON + timedelta(days=6), 1, None)[0]["line"]["PTS"] == 15  # who-plays toggle

# accuracy replay = the site's recipe, the recent-games blend; season average and minutes factor stay to compare against
bt = __import__("backtest")
hist = [{"date": f"2026-11-{d:02d}", "MIN": m, **{k: 10.0 for k in bt.STATS}} for d, m in [(1, 30), (3, 30), (5, 30), (14, 40), (16, 40)]]
assert bt.site(hist, when="2026-11-18") == bt.blend(hist)
assert abs(bt.site(hist)["PTS"] - 50 / 170 * (30 + 40 + 40) / 3) < 1e-9  # per-minute rate x minutes in the last 3 games
assert bt.season(hist)["PTS"] == 10
assert bt.minutes_factor(hist, when="2026-11-18")["PTS"] == 10 * 40 / 34  # Nov 14 + 16 in the last 7 days
# opponent projections use the same recipe as yours, role news included (full build)
fresh_news = {"role": 1, "headline": "Opp A will start", "published": (NOW - timedelta(days=1)).isoformat() + "Z"}
real_news = server.player_news
server.player_news = lambda league, roster: {p.playerId: [fresh_news] for p in roster}
league.scoreboard = lambda week: [NS(home_team=me, away_team=opp, home_team_cats=cats, away_team_cats=None)]
server.build_dashboard(league, me, NOW, {}, week=5)
assert opp.roster[0].proj_factor == 1.2 and "news: Opp A will start" in opp.roster[0].proj_why
server.build_dashboard(league, me, NOW, {}, week=5, actuals=False)  # quick first version: no news for anyone
assert opp.roster[0].proj_factor == 1.0
server.player_news = real_news
server._league["value"] = None; server._cache.clear(); server._news.clear()  # leave no fake state behind

# recent games: projection = blend of his finished games; news from before his last game no longer counts
gl = lambda d, mins, pts: {"date": d, "opp": "BOS", "at": "@", "result": "", "stats": {"MIN": mins, "FG%": "4-10", "FT%": "1-2",
                           "3PM": "1", "PTS": pts, "REB": "2", "AST": "1", "STL": "0", "BLK": "0", "TO": "1"}}
real_log, real_dt = server.fetch_gamelog, server.datetime
server.datetime = NS(now=lambda: NOW, fromisoformat=datetime.fromisoformat)
server.fetch_gamelog = lambda pid, season: [gl("2026-11-18", "30", "99"), gl("2026-11-17", "0", "0"),  # today's game + a DNP: skipped
                                            gl("2026-11-16", "20", "10"), gl("2026-11-14", "30", "20")] if pid == 90 else 1 / 0
streamer, nolog = f(90, "Streamer", [3], PTS=5, MIN=10), f(91, "No log", [3], PTS=7, MIN=10)
logs = server.game_logs([streamer, nolog, streamer])
assert [g["date"] for g in logs[90]] == ["2026-11-14", "2026-11-16"] and logs[91] == [], logs  # oldest first; ESPN error -> []
old_news = {"role": 1, "headline": "Streamer will start", "published": "2026-11-15T12:00:00Z"}  # before his 11/16 game
server.project_players([streamer, nolog], {90: [old_news]}, NOW, logs)
assert streamer.proj_basis == "recent" and streamer.proj_factor == 1.0, streamer.proj_why  # old news: already in his games
assert abs(streamer.proj["PTS"] - 30 / 50 * 25) < 1e-9  # 30 pts in 50 min, x 25 min in his last games
assert [g["date"] for g in streamer.recent] == ["2026-11-16", "2026-11-14"] and streamer.recent[0]["PTS"] == 10  # newest first
assert nolog.proj_basis == "season" and nolog.proj["PTS"] == 7 and nolog.recent == []  # no game log: ESPN season average
server.project_players([streamer], {90: [dict(old_news, published="2026-11-17T12:00:00Z")]}, NOW, logs)
assert streamer.proj_factor == 1.2  # news newer than his last game still counts
server.fetch_gamelog, server.datetime = real_log, real_dt

# with_live(): ESPN's totals (finished days) + today's starters, in-progress games included; % weighted by attempts
board = {"PTS": {"score": 380.0}, "TO": {"score": 60.0}, "FG%": {"score": 0.5}, "FT%": {"score": 0.8}}
ln = lambda **k: {"PTS": 0, "REB": 0, "AST": 0, "STL": 0, "BLK": 0, "3PM": 0, "TO": 0, "FGM": 0, "FGA": 0, "FTM": 0, "FTA": 0, **k}
lu = {MON: {1: {10: ln(PTS=100, FGM=40, FGA=80, FTA=0)}},  # earlier day: 80 attempts behind ESPN's .500
      TODAY: {1: {10: ln(PTS=30, TO=2, FGM=10, FGA=20, FTM=4, FTA=5), 11: None}, 2: {20: ln(PTS=9)}}}  # 11 = no stats yet
live = server.with_live(board, lu, [TODAY], 1)
assert live["PTS"]["score"] == 410 and live["TO"]["score"] == 62, live  # 380 + 30; only team 1's starters
assert abs(live["FG%"]["score"] - (0.5 * 80 + 10) / (80 + 20)) < 1e-9, live  # (40 + 10) / 100
assert live["FT%"]["score"] == 0.8 and board["PTS"]["score"] == 380  # no earlier FT attempts: ESPN's % kept; input untouched
assert server.with_live(board, lu, [TODAY + timedelta(days=1)], 1) is board  # day not in the week's data: unchanged
assert server.with_live(board, {TODAY: {1: {10: None}}}, [TODAY], 1) is board  # nobody has played yet today
first_fg = server.with_live({"FG%": {"score": 0}}, {TODAY: {1: {10: ln(FGM=3, FGA=4)}}}, [TODAY], 1)
assert first_fg["FG%"]["score"] == 0.75, first_fg  # first games of the week: today's % alone
# dates -> ESPN scoring periods come from ESPN's schedule, not from "today = current period": ESPN's current period
# rolls over hours after midnight, and until then every day would read the day before's lineup
ms = lambda d, h=19: datetime(d.year, d.month, d.day, h).timestamp() * 1000
sched = {1: {"40": [{"date": ms(MON)}], "42": [{"date": ms(MON + timedelta(days=2))}]}, 2: {"41": [{"date": ms(MON + timedelta(days=1))}]}}
morning = NS(pro_schedule=sched, scoringPeriodId=40)  # it's Tuesday, but ESPN still says Monday's period
TUE = MON + timedelta(days=1)
assert [server.scoring_period(morning, MON + timedelta(days=i), TUE) for i in range(4)] == [40, 41, 42, 43]  # Thu: no games, counts on
assert server.period_date(morning, 41, TUE) == TUE and server.period_date(morning, 44, TUE) == MON + timedelta(days=4)
lu2 = {MON: {}, TUE: {}, MON + timedelta(days=2): {}}
assert server.live_days(morning, lu2, TUE) == [MON, TUE]  # Monday isn't in ESPN's totals yet, so it's added too
assert server.live_days(NS(pro_schedule=sched, scoringPeriodId=41), lu2, TUE) == [TUE]
assert server.scoring_period(NS(pro_schedule={}, scoringPeriodId=40), TUE, TUE) == 40  # no schedule: today = ESPN's period
two = server.with_live(board, {MON: {1: {10: ln(PTS=20)}}, TUE: {1: {10: ln(PTS=5)}}}, [MON, TUE], 1)
assert two["PTS"]["score"] == 405, two  # both days' stats on top of ESPN's totals

# daily lineup page: every planner row has its per-game value and eligible slots; the dashboard carries the slots
assert all("value" in r and "elig" in r for r in d["planner"]["mine"]) and "lineup" in d, d["planner"]["mine"][0].keys()
slots_league = NS(espn_request=NS(league_get=lambda params: {"settings": {"rosterSettings": {"lineupSlotCounts":
                  {"0": 1, "1": 1, "2": 1, "3": 1, "4": 1, "5": 1, "10": 1, "11": 3, "12": 3, "13": 1}}}}))
assert server.lineup_slots(slots_league) == ["PG", "SG", "SF", "PF", "C", "G", "F/C", "UT", "UT", "UT"]  # BE and IR left out

# lineup page: each player's ESPN slot per day comes from that day's roster (None = not on the team that day)
sp = NS(playerId=7, lineupSlot="BE")
assert server.slot_on(sp, 1, TODAY, {TODAY: {1: {7: {"lineupSlotId": 11}}}}) == "UT"  # moved into a UTIL spot for that day
assert server.slot_on(sp, 1, TODAY, {TODAY: {1: {}}}) is None and server.slot_on(sp, 1, TODAY, {}) == "BE"  # dropped / no data

# IR: open spots = league's IR count - players already there; candidates = OUT players not on IR yet
irl = NS(espn_request=NS(league_get=lambda params: {"settings": {"rosterSettings": {"lineupSlotCounts": {"0": 1, "13": 2}}}}))
team = [NS(playerId=1, lineupSlot="IR", injuryStatus="OUT"), NS(playerId=2, lineupSlot="G", injuryStatus="OUT"),
        NS(playerId=3, lineupSlot="BE", injuryStatus="DAY_TO_DAY")]
assert server.ir_open(irl, team) == (1, {2})

print("ok")
