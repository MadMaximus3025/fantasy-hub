"""Accuracy check for per-game stat projections.

Replays real NBA games: for each game, every method predicts the player's line using only his earlier games that
season, then the prediction is compared with what he actually did. Lower error = better method.

Run: python backtest.py            (downloads ~360 ESPN game logs once, cached in .backtest_cache/)
     python backtest.py --per-team 3 --seasons 2026
"""
import argparse
import json
import pathlib
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from statistics import mean, pstdev
from urllib.request import Request, urlopen

GAMELOG_URL = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{}/gamelog?season={}"
ROSTER_URL = "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{}/roster"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
CACHE = pathlib.Path(__file__).with_name(".backtest_cache")
STATS = ["PTS", "REB", "AST", "STL", "BLK", "3PM", "TO", "FGM", "FGA", "FTM", "FTA"]
CATS = ["PTS", "REB", "AST", "STL", "BLK", "3PM", "TO", "FG%", "FT%"]
MIN_HISTORY = 10  # a game is scored once the player has this many earlier games that season


def fetch(url, name):
    """GET json, cached on disk by name."""
    path = CACHE / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text())
    with urlopen(Request(url, headers=UA), timeout=20) as r:
        data = json.load(r)
    CACHE.mkdir(exist_ok=True)
    path.write_text(json.dumps(data))
    return data


def games(log):
    """Regular season + playoff games from a raw ESPN game log, oldest first, as numbers."""
    labels, events, out = log.get("labels", []), log.get("events", {}), []
    for st in log.get("seasonTypes", []):
        if not st.get("displayName", "").endswith(("Regular Season", "Postseason")):
            continue
        for cat in st.get("categories", []):
            for row in cat.get("events", []):
                s = dict(zip(labels, row.get("stats", [])))
                if not s:
                    continue
                num = lambda v: float(v) if v not in ("", "-", None) else 0.0
                fgm, fga = (num(x) for x in (s.get("FG") or "0-0").split("-"))
                ftm, fta = (num(x) for x in (s.get("FT") or "0-0").split("-"))
                out.append({"date": events.get(row["eventId"], {}).get("gameDate", ""), "MIN": num(s.get("MIN")),
                            "PTS": num(s.get("PTS")), "REB": num(s.get("REB")), "AST": num(s.get("AST")),
                            "STL": num(s.get("STL")), "BLK": num(s.get("BLK")), "3PM": num((s.get("3PT") or "0-0").split("-")[0]),
                            "TO": num(s.get("TO")), "FGM": fgm, "FGA": fga, "FTM": ftm, "FTA": fta})
    return sorted(out, key=lambda g: g["date"])


# ---- projection methods: each gets the player's earlier games (oldest first) and returns a stat line ----
def avg(h, keys=STATS):
    return {k: mean(g[k] for g in h) for k in keys}


def season(h, when=None):
    """Season average so far."""
    return avg(h)


def site(h, when=None):
    """The site's projection: season average x (recent minutes / season minutes), capped 0.5-1.6. 'Recent' = games in
    the 7 days before `when` (2+ games), else 15 days (3+), like ESPN's last-7/15-day splits the live site reads."""
    day = lambda g: date.fromisoformat(g["date"][:10])
    ref = date.fromisoformat(when[:10]) if when else day(h[-1]) + timedelta(days=1)
    season_min, f = mean(g["MIN"] for g in h), 1
    for days, need in ((7, 2), (15, 3)):
        recent = [g["MIN"] for g in h if 0 < (ref - day(g)).days <= days]
        if season_min and len(recent) >= need:
            f = max(0.5, min(1.6, mean(recent) / season_min))
            break
    return {k: v * f for k, v in avg(h).items()}


def blend(h, when=None, recent=7, w=0.4):
    """Per-minute rates: 60% season, 40% last 7 games; times recent minutes (last 3 games)."""
    def rates(gs):
        mins = sum(g["MIN"] for g in gs) or 1
        return {k: sum(g[k] for g in gs) / mins for k in STATS}
    s, r = rates(h), rates(h[-recent:])
    minutes = mean(g["MIN"] for g in h[-3:])
    return {k: ((1 - w) * s[k] + w * r[k]) * minutes for k in STATS}


def last5(h, when=None):
    """Average of the last 5 games."""
    return avg(h[-5:])


def learned(h, when=None, k=20, start=5):
    """The site model, corrected by the player's own track record: for each stat, actual / projected over his
    earlier scored games, trusted more as games add up (weight n / (n + k))."""
    proj = {key: 0.0 for key in STATS}
    got = {key: 0.0 for key in STATS}
    for j in range(start, len(h)):
        p = site(h[:j], when=h[j]["date"])
        for key in STATS:
            proj[key] += p[key]
            got[key] += h[j][key]
    n, base = max(0, len(h) - start), site(h, when)
    return {key: base[key] * (1 + ((got[key] / proj[key] - 1) if proj[key] else 0) * n / (n + k)) for key in STATS}


METHODS = {"site (now)": site, "season avg": season, "blend 60/40": blend, "last 5": last5, "site+learned": learned}
HINDSIGHT = "hindsight*"  # cheats with the whole season's average (future games included): a floor for average-based methods


def cats(line, fg, ft):
    """A stat line in the 9 categories; FG%/FT% as makes above an average shooter on the same attempts."""
    out = {c: line[c] for c in CATS[:7]}
    out["FG%"] = line["FGM"] - fg * line["FGA"]
    out["FT%"] = line["FTM"] - ft * line["FTA"]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--per-team", type=int, default=6, help="players sampled per NBA team (default 6)")
    ap.add_argument("--seasons", default="2026,2027", help="ESPN season years, e.g. 2026 = 2025-26 (default 2026,2027)")
    ap.add_argument("--min-mpg", type=float, default=20, help="only players averaging at least this many minutes (default 20)")
    args = ap.parse_args()
    seasons = [int(s) for s in args.seasons.split(",")]

    ids = []
    for team in range(1, 31):
        try:
            roster = fetch(ROSTER_URL.format(team), f"roster-{team}")
        except Exception as e:
            print(f"roster {team}: {e}")
            continue
        ids += [a["id"] for a in roster.get("athletes", [])[: args.per_team]]

    jobs = [(pid, s) for pid in ids for s in seasons]

    def load(job):
        pid, s = job
        try:
            return games(fetch(GAMELOG_URL.format(pid, s), f"log-{pid}-{s}"))
        except Exception:
            return []  # rookies / players without that season

    with ThreadPoolExecutor(8) as pool:
        logs = [g for g in pool.map(load, jobs) if g and mean(x["MIN"] for x in g) >= args.min_mpg]

    # league-average shooting in the sample, for the FG%/FT% impact
    allg = [g for log in logs for g in log]
    fg = sum(g["FGM"] for g in allg) / sum(g["FGA"] for g in allg)
    ft = sum(g["FTM"] for g in allg) / sum(g["FTA"] for g in allg)

    errors = {m: {c: [] for c in CATS} for m in [*METHODS, HINDSIGHT]}
    actuals = {c: [] for c in CATS}
    for log in logs:
        whole = cats(avg(log), fg, ft)
        for i in range(MIN_HISTORY, len(log)):
            actual = cats(log[i], fg, ft)
            for c in CATS:
                actuals[c].append(actual[c])
            for name, method in METHODS.items():
                pred = cats(method(log[:i], when=log[i]["date"]), fg, ft)
                for c in CATS:
                    errors[name][c].append(abs(pred[c] - actual[c]))
            for c in CATS:
                errors[HINDSIGHT][c].append(abs(whole[c] - actual[c]))

    n = len(actuals["PTS"])
    spread = {c: pstdev(actuals[c]) or 1 for c in CATS}  # to put categories on one scale
    print(f"\n{len(logs)} player-seasons, {n} games scored (each with {MIN_HISTORY}+ earlier games), seasons {seasons}")
    print("Average miss per game (lower is better). 'Overall' = each category's miss / its spread, averaged.\n")
    head = f"{'method':<14}" + "".join(f"{c:>7}" for c in CATS) + f"{'overall':>9}"
    print(head)
    print("-" * len(head))
    overall = {}
    for name in [*METHODS, HINDSIGHT]:
        maes = {c: mean(errors[name][c]) for c in CATS}
        overall[name] = mean(maes[c] / spread[c] for c in CATS)
        print(f"{name:<14}" + "".join(f"{maes[c]:>7.2f}" for c in CATS) + f"{overall[name]:>9.3f}")
    best = min((m for m in overall if m != HINDSIGHT), key=overall.get)
    base = overall["site (now)"]
    print(f"\nBest: {best} ({(base - overall[best]) / base * 100:+.1f}% vs site (now))" if best != "site (now)" else "\nBest: site (now)")
    print("FG%/FT% columns are makes above an average shooter on the same attempts.")
    print(f"* {HINDSIGHT} uses each player's full-season average (it sees the future): roughly the best an average-based "
          "method can do; the gap to it is the room left for improvement.")


if __name__ == "__main__":
    main()
