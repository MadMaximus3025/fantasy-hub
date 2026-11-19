"""Streaming planner: pick the week's best free-agent adds (and who to drop) to win the most categories.

Pure functions only; server.py gathers the ESPN data. A "line" is one player's expected stats for one game,
with the keys of season averages (PTS, REB, ..., FGM, FGA, FTM, FTA).
"""

COUNTING = ["PTS", "REB", "AST", "STL", "BLK", "3PM", "TO"]
KEYS = COUNTING + ["FGM", "FGA", "FTM", "FTA"]
CATS = ["FG%", "FT%", "3PM", "PTS", "REB", "AST", "STL", "BLK", "TO"]


def totals(current, players):
    """Projected category totals: current matchup scores + every counted game line. FG%/FT% from remaining games."""
    t = {k: 0.0 for k in KEYS}
    for p in players:
        for line in p["games"].values():
            for k in KEYS:
                t[k] += line.get(k, 0)
    out = {k: t[k] + current.get(k, 0) for k in COUNTING}
    # same simplification as the dashboard projection: ESPN gives no makes/attempts so far this week
    out["FG%"] = t["FGM"] / t["FGA"] if t["FGA"] else current.get("FG%", 0)
    out["FT%"] = t["FTM"] / t["FTA"] if t["FTA"] else current.get("FT%", 0)
    return out


def result(mine, theirs):
    """W/L/T per category (TO: fewer wins)."""
    res = {}
    for c in CATS:
        a, b = mine[c], theirs[c]
        if c == "TO":
            a, b = -a, -b
        res[c] = "W" if a > b + 1e-9 else "L" if b > a + 1e-9 else "T"
    return res


def score(mine, theirs):
    """(categories won, ties count half; then total relative margin) - higher is better."""
    res = result(mine, theirs)
    wins = sum(r == "W" for r in res.values()) + 0.5 * sum(r == "T" for r in res.values())
    margin = 0.0
    for c in CATS:
        a, b = mine[c], theirs[c]
        d = (a - b) / (abs(a) + abs(b) + 1e-9)
        margin += -d if c == "TO" else d
    return wins, margin


def rows(players, tags=None):
    """What each player adds to the projection: games counted and the summed stat lines (for the breakdown table)."""
    out = []
    for p in players:
        tag = (tags or {}).get(p["id"])
        if not p["games"] and not tag:
            continue
        name = p["name"] + (f" · {tag}" if tag else "")
        out.append({"name": name, "games": len(p["games"]), **{k: sum(l.get(k, 0) for l in p["games"].values()) for k in KEYS}})
    return out


def record(res):
    return f'{sum(r == "W" for r in res.values())}-{sum(r == "L" for r in res.values())}-{sum(r == "T" for r in res.values())}'


def plan(days, slots, mine, opp_totals, current, free_agents, droppable, acquisitions):
    """Greedy week plan.

    days: remaining dates in order. slots: starting spots per day.
    mine: [{"id", "name", "games": {date: line}}] - games that count for you (starter, not OUT, not started yet).
    free_agents: [{"id", "name", "lines": {date: line}}] - every remaining game of each free agent.
    droppable: ids you are willing to drop. acquisitions: adds you may still make this week.
    Each step tries every add (on one of the free agent's game days) with every allowed drop that day,
    and keeps the move that raises the projected category score most. Stops when no move helps.
    """
    roster = [dict(p, games=dict(p["games"])) for p in mine]
    used = {d: sum(d in p["games"] for p in roster) for d in days}
    added_on = {}  # free agents you add can be dropped again later (streaming), but only after the day they were added
    dropped = set()  # each player can be dropped only once
    moves = []
    best = score(totals(current, roster), opp_totals)
    start = totals(current, roster)

    def apply(add, drop, day):
        """New roster and slot use after dropping `drop` and adding `add` on `day` (both from that day on)."""
        new_used = dict(used)
        new_roster = []
        for p in roster:
            if p["id"] == drop["id"]:
                for d in p["games"]:
                    if d >= day:
                        new_used[d] -= 1
                p = dict(p, games={d: line for d, line in p["games"].items() if d < day})
            new_roster.append(p)
        games = {}
        for d, line in add["lines"].items():
            if d >= day and new_used[d] < slots:  # only counts on days a starting spot is free
                games[d] = line
                new_used[d] += 1
        new_roster.append({"id": add["id"], "name": add["name"], "games": games})
        return new_roster, new_used, games

    for _ in range(max(0, acquisitions)):
        pick = None
        taken = {p["id"] for p in roster}
        for fa in free_agents:
            if fa["id"] in taken:
                continue
            for day in sorted(fa["lines"]):
                for drop in roster:
                    if drop["id"] in dropped:
                        continue
                    if drop["id"] in added_on:
                        if added_on[drop["id"]] >= day:  # a streamer can only be dropped after the day you added him
                            continue
                    elif drop["id"] not in droppable:
                        continue
                    new_roster, new_used, games = apply(fa, drop, day)
                    if not games:
                        continue
                    s = score(totals(current, new_roster), opp_totals)
                    if s > best and (pick is None or s > pick[0]):
                        pick = (s, fa, drop, day, new_roster, new_used, games)
        if not pick:
            break
        s, fa, drop, day, roster, used, games = pick
        best = s
        added_on[fa["id"]] = day
        dropped.add(drop["id"])
        moves.append({"day": day, "add": {"id": fa["id"], "name": fa["name"]}, "drop": {"id": drop["id"], "name": drop["name"]},
                      "games": sorted(games)})
    end = totals(current, roster)
    tags = {m["add"]["id"]: f"added {m['day'].strftime('%a %m/%d')}" for m in moves}
    for m in moves:  # a streamer added and later dropped shows both
        tags[m["drop"]["id"]] = (tags.get(m["drop"]["id"], "") + "; " if m["drop"]["id"] in tags else "") + f"dropped {m['day'].strftime('%a %m/%d')}"
    return {"moves": sorted(moves, key=lambda m: m["day"]),
            "current": current,
            "players": rows(roster, tags),
            "before": {"mine": start, "result": result(start, opp_totals), "record": record(result(start, opp_totals))},
            "after": {"mine": end, "result": result(end, opp_totals), "record": record(result(end, opp_totals))},
            "theirs": opp_totals}
