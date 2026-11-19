"""Self-check for the streaming planner (no network). Run: python test_streaming.py"""
from datetime import date, timedelta

import streaming as st

D = [date(2026, 11, 19) + timedelta(days=i) for i in range(4)]  # Thu..Sun
line = lambda **k: {**{x: 0 for x in st.KEYS}, **k}
big = line(PTS=20, REB=10, AST=5, STL=1, BLK=1, **{"3PM": 2}, TO=2, FGM=8, FGA=16, FTM=4, FTA=5)
me = [{"id": 1, "name": "Starter", "games": {D[0]: big}},
      {"id": 2, "name": "Scrub", "games": {D[0]: line(PTS=2, FGM=1, FGA=5)}}]
opp = {"FG%": .45, "FT%": .75, "3PM": 30, "PTS": 300, "REB": 120, "AST": 70, "STL": 20, "BLK": 10, "TO": 40}
cur = {"PTS": 250, "REB": 100, "AST": 60, "STL": 15, "BLK": 8, "3PM": 20, "TO": 35}
fa = [{"id": 10, "name": "Streamer", "lines": {D[1]: big, D[2]: big, D[3]: big}},
      {"id": 11, "name": "Dud", "lines": {D[1]: line(PTS=1, FGM=0, FGA=4)}}]

# baseline: losing most categories
p0 = st.plan(D, 2, me, opp, cur, fa, droppable=set(), acquisitions=3)
assert p0["moves"] == [], "nothing droppable -> no moves"

p = st.plan(D, 2, me, opp, cur, fa, droppable={2}, acquisitions=3)
m = p["moves"]
assert m[0]["add"]["name"] == "Streamer" and m[0]["drop"]["name"] == "Scrub", m
assert m[0]["day"] <= D[1] and m[0]["games"] == D[1:], m  # added by his first game day, counts all three games
scrub_thu = 2 if m[0]["day"] > D[0] else 0  # dropping Scrub today also drops his (poor-shooting) Thursday game
assert p["after"]["mine"]["PTS"] == cur["PTS"] + 20 + scrub_thu + 60, p["after"]
assert p["after"]["record"] != p["before"]["record"]
assert all(x["add"]["name"] != "Dud" for x in m), "a move that doesn't help is never made"

# acquisitions cap respected
assert st.plan(D, 2, me, opp, cur, fa, droppable={2}, acquisitions=0)["moves"] == []

# a full lineup gives a pickup nothing: slots=1 already used every day by a starter with games
full = [{"id": 1, "name": "Starter", "games": {d: big for d in D}}, {"id": 2, "name": "Bench", "games": {}}]
assert st.plan(D, 1, full, opp, cur, fa, droppable={2}, acquisitions=3)["moves"] == []

# only moves that win another category: a pickup that just narrows a gap isn't suggested
small = [{"id": 30, "name": "Tiny", "lines": {D[1]: line(PTS=1)}}]
empty = me + [{"id": 3, "name": "Empty", "games": {}}]  # dropping him changes nothing by itself
assert st.plan(D, 3, empty, opp, cur, small, droppable={3}, acquisitions=5)["moves"] == []
# as many adds as help, never more than allowed
many = [{"id": 40 + i, "name": f"S{i}", "lines": {D[1]: big, D[2]: big, D[3]: big}} for i in range(5)]
pm = st.plan(D, 9, me, opp, cur, many, droppable={1, 2}, acquisitions=1)
assert len(pm["moves"]) == 1

# TO: fewer wins
assert st.result({**opp, "TO": 10}, opp)["TO"] == "W"
# each player is dropped at most once, and moves come back in day order
fa2 = fa + [{"id": 12, "name": "Thu Guy", "lines": {D[0]: big}}]
p2 = st.plan(D, 3, me, opp, cur, fa2, droppable={2}, acquisitions=3)
drops_made = [x["drop"]["id"] for x in p2["moves"]]
assert len(drops_made) == len(set(drops_made)), p2["moves"]
assert [x["day"] for x in p2["moves"]] == sorted(x["day"] for x in p2["moves"])
# a float adds count (ESPN sends 6.0) works
assert st.plan(D, 2, me, opp, cur, fa, droppable={2}, acquisitions=6.0)["moves"]
# overall value: a clearly better player is an upgrade even if no category flips this week
D2 = D[:1]
tied = {c: 0 for c in st.CATS}
# upgrades count games this week + next week: no games left now but 4 next week still helps
weak, strong = [{"id": 1, "name": "Weak", "games": {}}], [{"id": 5, "name": "Strong", "lines": {}}]
up = st.plan(D2, 1, weak, tied, {}, strong, droppable={1}, acquisitions=2, values={1: -0.5, 5: 1.0}, next_games={1: 4, 5: 4})
assert [(x["kind"], x["add"]["name"], x["value"], x["next_games"]) for x in up["moves"]] == [("upgrade", "Strong", 6.0, 4)], up["moves"]
assert (up["moves"][0]["add"]["value"], up["moves"][0]["drop"]["value"]) == (1.0, -0.5)  # per-game 9-cat values for the chart
# no games this week or next: can't help, never suggested however good
assert st.plan(D2, 1, weak, tied, {}, strong, droppable={1}, acquisitions=2, values={1: -2.0, 5: 3.0}, next_games={1: 4})["moves"] == []
# a small two-week gain doesn't use an add
assert st.plan(D2, 1, weak, tied, {}, strong, droppable={1}, acquisitions=2, values={1: 0.0, 5: 0.3}, next_games={1: 4, 5: 4})["moves"] == []
# fewer games next week can outweigh better per-game value: 1.0 x 1 game < 0.4 x 4 games
assert st.plan(D2, 1, weak, tied, {}, strong, droppable={1}, acquisitions=2, values={1: 0.4, 5: 1.0}, next_games={1: 4, 5: 1})["moves"] == []
# a stream that would drop a much better player isn't made, even if it wins a category this week
star = [{"id": 1, "name": "Star", "games": {}}]
sfa = [{"id": 5, "name": "Streamer", "lines": {D[1]: big}}]
assert st.plan(D, 1, star, opp, cur, sfa, droppable={1}, acquisitions=2, values={1: 4.0, 5: 0.0}, next_games={1: 4})["moves"] == []
assert st.plan(D, 1, star, opp, cur, sfa, droppable={1}, acquisitions=2, values={1: 0.5, 5: 0.0}, next_games={1: 4})["moves"][0]["kind"] == "stream"

# player values: better per-game lines score higher; efficient shooting counts; turnovers hurt
lines = {1: line(PTS=25, REB=8, AST=6, STL=1, BLK=1, **{"3PM": 3}, TO=2, FGM=9, FGA=17, FTM=4, FTA=5),
         2: line(PTS=8, REB=3, AST=1, TO=1, FGM=3, FGA=9, FTM=1, FTA=2),
         3: line(PTS=8, REB=3, AST=1, TO=4, FGM=3, FGA=9, FTM=1, FTA=2)}
v = st.player_values(lines)
assert v[1] > v[2] > v[3], v
print("ok")
