"""Streaming planner: pick the week's best free-agent adds (and who to drop) to win the most categories.

Pure functions only; server.py gathers the ESPN data. A "line" is one player's expected stats for one game,
with the keys of season averages (PTS, REB, ..., FGM, FGA, FTM, FTA).
"""

from statistics import NormalDist

COUNTING = ["PTS", "REB", "AST", "STL", "BLK", "3PM", "TO"]
KEYS = COUNTING + ["FGM", "FGA", "FTM", "FTA"]
CATS = ["FG%", "FT%", "3PM", "PTS", "REB", "AST", "STL", "BLK", "TO"]


# Win this week first: a move is judged by how many categories you expect to win this week (sum of each category's
# win chance, the dashboard's odds model). Next week only counts once this week can't be helped.
# shortcut: hand-picked thresholds; tune if moves feel off. Values = per-game value x games this week + next.
STREAM_MIN = 0.10       # a stream must add at least this many expected categories this week
STREAM_TOLERANCE = 4.0  # ...and may cost at most this much two-week value (about 1 per game over ~4 games)
UPGRADE_MIN = 2.0       # an upgrade for next week must add at least this much two-week value
LOCKED_COST = 0.02      # ...and may cost this week at most this many expected categories (locked ones only)
PROTECT_WEIGHT = 0.3    # with the opponent's worst case: 70% "he stands pat", 30% "he streams as well as he can"
SPREAD = {"PTS": 2, "REB": 1.5, "AST": 1.5, "STL": 1.2, "BLK": 1.5, "3PM": 1.3, "TO": 1.2}  # same as the dashboard


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


def remaining(players):
    """Summed lines of every counted game still to come (what can still move the totals)."""
    return {k: sum(l.get(k, 0) for p in players for l in p["games"].values()) for k in KEYS}


def odds(mine, theirs, rem_mine, rem_theirs=None):
    """Chance of winning each category this week: projected margin against how much it usually swings over the
    games left (the dashboard's model). FG%/FT% swing with attempts left."""
    rem_theirs = rem_theirs or {}
    prob = {}
    for c in CATS:
        diff = mine[c] - theirs[c]
        if c == "TO":
            diff = -diff
        if c.endswith("%"):
            k = "FGA" if c == "FG%" else "FTA"
            v = sum(p * (1 - p) / n for p, n in ((mine[c], rem_mine.get(k, 0)), (theirs[c], rem_theirs.get(k, 0))) if n)
        else:
            v = SPREAD[c] * (rem_mine.get(c, 0) + rem_theirs.get(c, 0))
        prob[c] = NormalDist().cdf(diff / v ** 0.5) if v > 0 else 1.0 if diff > 1e-9 else 0.0 if diff < -1e-9 else 0.5
    return prob


def win_chance(prob):
    """Chance of winning the matchup (5+ of 9 categories), treating categories as independent."""
    dist = [1.0]
    for c in CATS:
        dist = [x * (1 - prob[c]) + (dist[k - 1] * prob[c] if k else 0) for k, x in enumerate(dist + [0.0])]
    return sum(dist[5:])


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


def fits(players, lineup):
    """Can every one of `players` (each a list of eligible slot names) start at once in `lineup` (slot names)?
    Matching by augmenting paths; lineups have about ten slots, so this is instant."""
    owner = {}  # slot index -> player index

    def place(i, seen):
        for j, slot in enumerate(lineup):
            if slot in players[i] and j not in seen:
                seen.add(j)
                if j not in owner or place(owner[j], seen):
                    owner[j] = i
                    return True
        return False

    return len(players) <= len(lineup) and all(place(i, set()) for i in range(len(players)))


def record(res):
    return f'{sum(r == "W" for r in res.values())}-{sum(r == "L" for r in res.values())}-{sum(r == "T" for r in res.values())}'


def plan(days, slots, mine, opp_totals, current, free_agents, droppable, acquisitions, values=None, next_games=None,
         opp_rem=None, lineup=None, ir_spots=0, ir_ids=None, opp_worst=None, protect=PROTECT_WEIGHT):
    """Greedy week plan.

    days: remaining dates in order. slots: starting spots per day.
    mine: [{"id", "name", "games": {date: line}}] - games that count for you (starter, not OUT, not started yet).
    free_agents: [{"id", "name", "lines": {date: line}}] - every remaining game of each free agent.
    droppable: ids you are willing to drop. acquisitions: the most adds you may still make this week.
    values: {id: per-game season value} from player_values(). next_games: {id: games in next week's matchup}.
    A move's value gain = pickup's value x his games (counted this week + next week) - the same for the dropped player.
    opp_rem: the opponent's summed remaining lines (remaining()), for how much his totals can still swing.
    lineup: the league's starting slots (e.g. ["PG", "SG", ..., "UT"]); with players' "elig" slot lists, a pickup's
    game only counts on a day he fits into the lineup next to everyone already starting (positions checked).
    Without it, a game counts while fewer than `slots` players start that day.
    opp_worst: {"totals", "rem"} of the opponent after his best streaming (Scouting's worst case). Then every chance is
    the `protect` blend (default PROTECT_WEIGHT) of "he stands pat" and "he streams hard", so moves that shore up the categories he could
    take count more; moves report the categories they protect.
    ir_spots / ir_ids: open IR spots and OUT players not on IR yet. Moving one to IR frees a roster spot like a drop
    (he plays no games either way) but keeps him, so it costs no value; at most ir_spots such moves are made.
    Each step tries every add (today or on one of the free agent's game days) with every allowed drop. This week
    comes first: a stream raises the categories you expect to win this week (by STREAM_MIN+) without costing much
    two-week value, and the best stream wins. Only when no stream helps, an upgrade may be made: clear value this
    week + next that costs this week nothing but locked categories. Stops when no move qualifies or adds run out.
    """
    values, next_games = values or {}, next_games or {}
    ir_ids = set(ir_ids or ())
    ir_left = [ir_spots]  # IR spots still open
    roster = [dict(p, games=dict(p["games"])) for p in mine]
    used = {d: sum(d in p["games"] for p in roster) for d in days}
    added_on = {}  # free agents you add can be dropped again later (streaming), but only after the day they were added
    dropped = set()  # each player can be dropped only once
    moves = []
    best = score(totals(current, roster), opp_totals)
    start = totals(current, roster)
    def chances(r):
        base = odds(totals(current, r), opp_totals, remaining(r), opp_rem)
        if not opp_worst:
            return base
        w = worst_odds(r)
        return {c: (1 - protect) * base[c] + protect * w[c] for c in CATS}

    worst_odds = lambda r: odds(totals(current, r), opp_worst["totals"], remaining(r), opp_worst["rem"])
    plain = lambda r: odds(totals(current, r), opp_totals, remaining(r), opp_rem)  # what the pages show
    best_prob = chances(roster)

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
            if d < day:
                continue
            if lineup and add.get("elig") is not None:  # positions: he must fit next to that day's starters
                starters = [p.get("elig") or [] for p in new_roster if d in p["games"]]
                ok = fits(starters + [add["elig"]], lineup) if fits(starters, lineup) else new_used[d] < slots
            else:
                ok = new_used[d] < slots  # only counts on days a starting spot is free
            if ok:
                games[d] = line
                new_used[d] += 1
        new_roster.append({"id": add["id"], "name": add["name"], "games": games,
                           **{k: add.get(k) for k in ("note", "per_game", "season", "factor", "basis", "recent", "upcoming", "elig")}})
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
                    elif drop["id"] not in droppable and not (drop["id"] in ir_ids and ir_left[0] > 0):
                        continue
                    new_roster, new_used, games = apply(fa, drop, day)
                    s = score(totals(current, new_roster), opp_totals)
                    dw = s[0] - best[0]  # categories flipped this week (ties count half), for display
                    prob = chances(new_roster)
                    de = sum(prob.values()) - sum(best_prob.values())  # expected categories gained this week
                    # value gained over the rest of this week and next week: per-game value x games each player plays
                    add_games = len(games) + next_games.get(fa["id"], 0)
                    drop_games = sum(d >= day for d in drop["games"]) + next_games.get(drop["id"], 0)
                    if not add_games:
                        continue  # can't help this week or next
                    dv = values.get(fa["id"], 0) * add_games - values.get(drop["id"], 0) * drop_games
                    is_stream = de >= STREAM_MIN and games and dv > -STREAM_TOLERANCE
                    is_upgrade = de >= -LOCKED_COST and dv >= UPGRADE_MIN
                    if not (is_stream or is_upgrade):
                        continue
                    # any stream beats any upgrade; streams by this week's gain, upgrades by two-week value
                    key = (1, de, dv) if is_stream else (0, dv, de)
                    if pick is None or key > pick[0]:
                        pick = (key, s, dw, dv, fa, drop, day, new_roster, new_used, games, add_games, drop_games, prob, de)
        if not pick:
            break
        before = totals(current, roster)
        before_roster = roster
        _, s, dw, dv, fa, drop, day, roster, used, games, add_games, drop_games, prob, de = pick
        prob_before = best_prob
        best_prob = prob
        # categories this move rescues from his best streaming (10+ points of win chance in that scenario)
        protects = [] if not opp_worst else [c for c, (a, b) in
                                             ((c, (worst_odds(before_roster)[c], worst_odds(roster)[c])) for c in CATS) if b - a >= 0.10]
        after = totals(current, roster)
        res_before, res_after = result(before, opp_totals), result(after, opp_totals)
        best = s
        added_on[fa["id"]] = day
        dropped.add(drop["id"])
        to_ir = drop["id"] in ir_ids and ir_left[0] > 0  # an OUT player goes to IR instead of being released
        ir_left[0] -= to_ir
        # display: the per-game projection the plan used, why, his last games and his games ahead
        side = lambda p: {"id": p["id"], "name": p["name"], "line": p.get("per_game") or p.get("season"),
                          "factor": p.get("factor") or 1, "note": p.get("note") or "", "value": round(values.get(p["id"], 0), 1),
                          **{k: p.get(k) for k in ("basis", "recent", "upcoming")}}
        lost = {d: l for d, l in drop["games"].items() if d >= day}  # the dropped player's games from that day on
        moves.append({"day": day, "add": side(fa), "drop": side(drop),
                      "games": sorted(games), "why": fa.get("note", ""),
                      # his games from the add day on that can't count: lineup full, or no open slot for his positions
                      "benched": sorted(d for d in fa["lines"] if d >= day and d not in games),
                      "kind": "stream" if pick[0][0] else "upgrade", "week": dw, "value": round(dv, 2), "ir": to_ir,
                      "protects": protects,
                      # what each side brings to or takes from this week's matchup: games and summed stats
                      "add_stats": {k: sum(l.get(k, 0) for l in games.values()) for k in KEYS},
                      "drop_stats": {k: sum(l.get(k, 0) for l in lost.values()) for k in KEYS},
                      "expected": [round(sum(plain(before_roster).values()), 2), round(sum(plain(roster).values()), 2)],
                      "win": [round(win_chance(plain(before_roster)), 3), round(win_chance(plain(roster)), 3)],
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
            # same odds as the dashboard: categories expected = each category's chance of being won, summed
            "before": {"mine": start, "result": result(start, opp_totals), "record": record(result(start, opp_totals)),
                       "expected": round(sum(plain(mine).values()), 2), "prob": plain(mine)},
            "after": {"mine": end, "result": result(end, opp_totals), "record": record(result(end, opp_totals)),
                      "expected": round(sum(plain(roster).values()), 2), "prob": plain(roster)},
            "theirs": opp_totals}
