"""Self-check with fake ESPN objects (no network). Run: python test_server.py"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace as NS

import server

TODAY = date(2026, 11, 18)  # Wednesday
MON = TODAY - timedelta(days=2)


def player(pid, name, game_days, injury="ACTIVE", **avg):
    sched = {str(i): {"team": "BOS", "date": datetime.combine(MON + timedelta(days=d), datetime.min.time())}
             for i, d in enumerate(game_days)}
    avg = {"FGM": 8, "FGA": 16, "FTM": 4, "FTA": 5, "PTS": 22, "REB": 6, "AST": 5,
           "STL": 1, "BLK": 1, "3PM": 2, "TO": 3, **avg}
    return NS(playerId=pid, name=name, year=2027, schedule=sched, injuryStatus=injury,
              eligibleSlots=["PG", "SG", "G", "UT"], proTeam="ATL",
              stats={"2027_total": {"avg": avg}},
              nine_cat_averages={k: v for k, v in avg.items() if k in server.CATS})


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

d = server.build_dashboard(league, me, TODAY, {1: [{"headline": "h", "story": "s"}]})
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

# bye week: no matchup
league.scoreboard = lambda: []
assert server.build_dashboard(league, me, TODAY, {})["opponent"] is None
print("ok")
