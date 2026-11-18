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
league = NS(settings=NS(name="Family n Friends"), teams=[me, opp],
            scoreboard=lambda: [NS(home_team=me, away_team=opp, home_team_cats=cats, away_team_cats=None)])

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
assert server.daily_slate([f(1, "a", [3]), f(2, "b", [3], slot="BE"), f(3, "c", [3], slot="IR")], 1, MON, TODAY) == [0, 0, 0, 1, 0, 0, 0]
assert server.project([f(1, "a", [3]), f(2, "b", [3], slot="BE")], None, NOW, MON + timedelta(days=6))["PTS"] == 22

# per-day ESPN lineups: Tuesday (past) counts starters who actually played; later days use that day's lineup
TUE, THU = MON + timedelta(days=1), MON + timedelta(days=3)
a, b, c = f(1, "a", [1, 3], slot="BE"), f(2, "b", [1, 3]), f(3, "c", [1, 3], injury="OUT")
lu = {TUE: {1: {1: True, 2: True, 3: False}}, THU: {1: {1: True, 3: True}}}  # Tue: a & b played, c (OUT) didn't; Thu: a & c start
assert server.daily_slate([a, b, c], 1, MON, TODAY, lu) == [0, 2, 0, 1, 0, 0, 0]  # Thu: c is OUT, b benched
rows = server.contributions([a, b, c], NOW, MON + timedelta(days=6), 1, lu)
assert [(r["name"], r["games"]) for r in rows] == [("a", 1)], rows  # only a starts Thursday and is available

# played(): stats recorded in that period count; projections or empty stat lines don't
e = lambda *s: {"playerPoolEntry": {"player": {"stats": list(s)}}}
assert server.played(e({"scoringPeriodId": 30, "statSourceId": 0, "stats": {"0": 12.0}}), 30)
assert not server.played(e({"scoringPeriodId": 30, "statSourceId": 1, "stats": {"0": 12.0}}), 30)
assert not server.played(e({"scoringPeriodId": 30, "statSourceId": 0, "stats": {"0": 0}}), 30)
assert not server.played(e({"scoringPeriodId": 29, "statSourceId": 0, "stats": {"0": 12.0}}), 30)

# day_lineups(): bench/IR dropped, period offset from today, teams without lineup data left out
def fake_get(params, headers):
    p = params["scoringPeriodId"]
    entry = lambda pid, slot: {"playerId": pid, "lineupSlotId": slot, "playerPoolEntry": {"player": {"stats": [{"scoringPeriodId": p, "statSourceId": 0, "stats": {"0": 5}}]}}}
    return {"schedule": [{"home": {"teamId": 1, "rosterForCurrentScoringPeriod": {"entries": [entry(p, 0), entry(99, 12), entry(98, 13)]}}, "away": {"teamId": 2}}]}
lg = NS(scoringPeriodId=30, currentMatchupPeriod=5, espn_request=NS(league_get=fake_get))
got = server.day_lineups(lg, MON, TODAY, {1, 2})
assert got[TODAY] == {1: {30: True}} and got[MON] == {1: {28: True}}, got  # today = period 30, Monday = 28
pro = {1: {"5": [{"date": datetime(2026, 11, 16, 19).timestamp() * 1000, "homeProTeamId": 1, "awayProTeamId": 2}]},
       2: {"5": [{"date": datetime(2026, 11, 16, 19).timestamp() * 1000, "homeProTeamId": 1, "awayProTeamId": 2}]}}
assert server.nba_games(NS(pro_schedule=pro), MON) == [1, 0, 0, 0, 0, 0, 0]  # same game listed under both teams counts once

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
server._cache["data"] = None
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
server._cache.update(at=_t.time() - 30, data={"cached": True})
assert server.dashboard(refresh=True) == {"cached": True}

# after tonight's games: Wednesday is already in the current totals, so it is not projected again
late = server.project(me.roster, cats, datetime(2026, 11, 18, 23), MON + timedelta(days=6))
assert late["PTS"] == 4 * 22 + 50, late["PTS"]  # Johnson Fri+Sun, Booker Thu+Sat; Wed not counted again
# bye week: no matchup
league.scoreboard = lambda: []
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
server._cache["data"] = {"roster": [{"id": 7}]}
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
server.fetch_gamelog = real_fetch
print("ok")
