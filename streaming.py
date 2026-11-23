"""Streaming planner: pick the week's best free-agent adds (and who to drop) to win the most categories.

Pure functions only; server.py gathers the ESPN data. A "line" is one player's expected stats for one game,
with the keys of season averages (PTS, REB, ..., FGM, FGA, FTM, FTA).
"""

COUNTING = ["PTS", "REB", "AST", "STL", "BLK", "3PM", "TO"]
KEYS = COUNTING + ["FGM", "FGA", "FTM", "FTA"]
CATS = ["FG%", "FT%", "3PM", "PTS", "REB", "AST", "STL", "BLK", "TO"]


# shortcut: hand-picked weights for trading this week's categories against season-long value; tune if moves feel off
# values below are per-game value x games over the rest of this week and next week's matchup
VALUE_WEIGHT = 0.15     # one category won this week counts like ~7 points of two-week value
STREAM_TOLERANCE = 4.0  # a stream may cost up to this much two-week value (about 1 per game over ~4 games)
UPGRADE_MIN = 2.0       # a move that wins nothing this week must add at least this much two-week value


def zscorer(lines):
    """Score any stat line per category against this pool of per-game lines: {cat: z-score}.
    TO counts negative; FG%/FT% are makes above what an average shooter makes on the same attempts."""
    pool = list(lines.values())
    fg = sum(l.get("FGM", 0) for l in pool) / (sum(l.get("FGA", 0) for l in pool) or 1)
    ft = sum(l.get("FTM", 0) for l in pool) / (sum(l.get("FTA", 0) for l in pool) or 1)

    def raw(l):
        r = {c: l.get(c, 0) for c in ("PTS", "REB", "AST", "STL", "BLK", "3PM")}
        r["TO"] = -l.get("TO", 0)
        r["FG%"] = l.get("FGM", 0) - fg * l.get("FGA", 0)
        r["FT%"] = l.get("FTM", 0) - ft * l.get("FTA", 0)
        return r

    rows = [raw(l) for l in pool]
    stats = {}
    for c in CATS:
        vals = [r[c] for r in rows]
        mean = sum(vals) / len(vals)
        stats[c] = (mean, (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5 or 1)
    return lambda l: {c: (v - stats[c][0]) / stats[c][1] for c, v in raw(l).items()}


def player_values(lines):
    """Per-game 9-cat value of each player: sum of z-scores against the pool (see zscorer)."""
    if len(lines) < 2:
        return {i: 0.0 for i in lines}
    z = zscorer(lines)
    return {i: sum(z(l).values()) for i, l in lines.items()}


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
        name = p["name"] + (f" · {tag}" if tag else "") + (f" ({p['note']})" if p.get("note") else "")
        out.append({"name": name, "games": len(p["games"]), **{k: sum(l.get(k, 0) for l in p["games"].values()) for k in KEYS}})
    return out


def record(res):
    return f'{sum(r == "W" for r in res.values())}-{sum(r == "L" for r in res.values())}-{sum(r == "T" for r in res.values())}'


def plan(days, slots, mine, opp_totals, current, free_agents, droppable, acquisitions, values=None, next_games=None):
    """Greedy week plan.

    days: remaining dates in order. slots: starting spots per day.
    mine: [{"id", "name", "games": {date: line}}] - games that count for you (starter, not OUT, not started yet).
    free_agents: [{"id", "name", "lines": {date: line}}] - every remaining game of each free agent.
    droppable: ids you are willing to drop. acquisitions: the most adds you may still make this week.
    values: {id: per-game season value} from player_values(). next_games: {id: games in next week's matchup}.
    A move's value gain = pickup's value x his games (counted this week + next week) - the same for the dropped player.
    Each step tries every add (today or on one of the free agent's game days) with every allowed drop, and makes
    the best move of two kinds: a stream (wins more categories this week without costing much two-week value)
    or an upgrade (adds clear value this week and/or next week, without losing a category this week). Moves rank by
    categories gained + VALUE_WEIGHT x value gained. Stops when no move qualifies or adds run out.
    """
    values, next_games = values or {}, next_games or {}
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
        new_roster.append({"id": add["id"], "name": add["name"], "games": games,
                           **{k: add.get(k) for k in ("note", "per_game", "season", "factor", "basis", "recent", "upcoming")}})
        return new_roster, new_used, games

    for _ in range(max(0, int(acquisitions))):
        pick = None
        taken = {p["id"] for p in roster}
        for fa in free_agents:
            if fa["id"] in taken:
                continue
            for day in sorted(set(fa["lines"]) | set(days[:1])):  # today too, for upgrades with no games left this week
                for drop in roster:
                    if drop["id"] in dropped:
                        continue
                    if drop["id"] in added_on:
                        if added_on[drop["id"]] >= day:  # a streamer can only be dropped after the day you added him
                            continue
                    elif drop["id"] not in droppable:
                        continue
                    new_roster, new_used, games = apply(fa, drop, day)
                    s = score(totals(current, new_roster), opp_totals)
                    dw = s[0] - best[0]  # categories gained this week (ties count half)
                    # value gained over the rest of this week and next week: per-game value x games each player plays
                    add_games = len(games) + next_games.get(fa["id"], 0)
                    drop_games = sum(d >= day for d in drop["games"]) + next_games.get(drop["id"], 0)
                    if not add_games:
                        continue  # can't help this week or next
                    dv = values.get(fa["id"], 0) * add_games - values.get(drop["id"], 0) * drop_games
                    is_stream = dw > 0 and games and dv > -STREAM_TOLERANCE
                    is_upgrade = dw >= 0 and dv >= UPGRADE_MIN
                    if not (is_stream or is_upgrade):
                        continue
                    key = (dw + VALUE_WEIGHT * dv, s[1])
                    if pick is None or key > pick[0]:
                        pick = (key, s, dw, dv, fa, drop, day, new_roster, new_used, games, add_games, drop_games)
        if not pick:
            break
        before = totals(current, roster)
        _, s, dw, dv, fa, drop, day, roster, used, games, add_games, drop_games = pick
        after = totals(current, roster)
        res_before, res_after = result(before, opp_totals), result(after, opp_totals)
        best = s
        added_on[fa["id"]] = day
        dropped.add(drop["id"])
        # display: the per-game projection the plan used, why, his last games and his games ahead
        side = lambda p: {"id": p["id"], "name": p["name"], "line": p.get("per_game") or p.get("season"),
                          "factor": p.get("factor") or 1, "note": p.get("note") or "", "value": round(values.get(p["id"], 0), 1),
                          **{k: p.get(k) for k in ("basis", "recent", "upcoming")}}
        moves.append({"day": day, "add": side(fa), "drop": side(drop),
                      "games": sorted(games), "why": fa.get("note", ""),
                      "kind": "stream" if dw > 0 else "upgrade", "week": dw, "value": round(dv, 2),
                      "next_games": next_games.get(fa["id"], 0),
                      # the numbers behind the move, for the "why" on each card
                      "add_games": add_games, "drop_games": drop_games,
                      "drop_week": sum(d >= day for d in drop["games"]), "drop_next": next_games.get(drop["id"], 0),
                      "change": {c: after[c] - before[c] for c in CATS},  # this week's projected totals, after - before
                      "flips": {c: [res_before[c], res_after[c]] for c in CATS if res_before[c] != res_after[c]}})
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
