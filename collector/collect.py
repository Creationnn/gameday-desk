#!/usr/bin/env python3
"""Gameday Desk data collector.

Gathers weekly fantasy projections from several independent sources, maps every
player to a Sleeper player id, and writes one JSON file the app reads:

    data/latest.json               current week (overwritten each run)
    data/history/<season>-wk<NN>.json   snapshot per week, kept for accuracy grading

Every source is stored as a raw stat line using Sleeper's stat keys (pass_yd,
rec, idp_sack, ...), so the app can score each source with any league's rules.

Sources
  sleeper  Sleeper's own projections
  espn     ESPN's projections (offense)
  model    Gameday Desk's own model, built from nflverse play-by-play stats

Standard library only, so the GitHub Action needs no installs.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import sys
import time
import unicodedata
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

UA = "gameday-desk-collector/1.0 (+https://github.com/Creationnn/gameday-desk)"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "data")

OFFENSE = ("QB", "RB", "WR", "TE")
IDP = ("DL", "LB", "DB")
POS_NORM = {"DE": "DL", "DT": "DL", "NT": "DL", "CB": "DB", "S": "DB", "SS": "DB", "FS": "DB",
            "SAF": "DB", "OLB": "LB", "ILB": "LB", "MLB": "LB", "FB": "RB"}
TEAM_NORM = {"LA": "LAR", "JAC": "JAX", "WSH": "WAS", "OAK": "LV", "SD": "LAC", "STL": "LAR"}


# ---------------------------------------------------------------- http helpers
def get(url: str, headers: dict | None = None, tries: int = 3, timeout: int = 60) -> bytes:
    h = {"User-Agent": UA, "Accept": "application/json, text/csv, */*"}
    h.update(headers or {})
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def get_json(url: str, headers: dict | None = None):
    return json.loads(get(url, headers))


def get_csv(url: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(get(url, timeout=120).decode("utf-8"))))


def num(x) -> float:
    try:
        v = float(x)
        return 0.0 if math.isnan(v) else v
    except (TypeError, ValueError):
        return 0.0


def clean(stats: dict) -> dict:
    return {k: round(v, 3) for k, v in stats.items() if isinstance(v, (int, float)) and abs(v) >= 0.0005}


def norm_pos(p: str) -> str:
    return POS_NORM.get(p, p)


def norm_team(t: str) -> str:
    return TEAM_NORM.get(t, t)


SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
ESPN_POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE"}
ESPN_TEAM = {1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN", 8: "DET", 9: "GB",
             10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LAR", 15: "MIA", 16: "MIN", 17: "NE", 18: "NO",
             19: "NYG", 20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC", 25: "SF", 26: "SEA", 27: "TB",
             28: "WAS", 29: "CAR", 30: "JAX", 33: "BAL", 34: "HOU"}


def name_key(name: str) -> str:
    n = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    parts = [w for w in re.sub(r"[^a-z ]", "", n.replace("-", " ")).split() if w not in SUFFIXES]
    return "".join(parts)


class Matcher:
    """Finds a Sleeper id from external ids first, then name + position, using team to break ties."""

    def __init__(self, players: dict):
        self.by_espn, self.by_gsis, self.by_name = {}, {}, defaultdict(list)
        for pid, p in players.items():
            if p.get("espn_id"):
                self.by_espn[str(p["espn_id"])] = pid
            if p.get("gsis_id"):
                self.by_gsis[str(p["gsis_id"]).strip()] = pid
            pos = norm_pos(p.get("position") or "")
            nm = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}"
            if pos and nm.strip():
                self.by_name[(name_key(nm), pos)].append((pid, p.get("team") or "", bool(p.get("active"))))
        self.via_name = 0

    def find(self, name: str, pos: str, team: str = "", espn: str = "", gsis: str = "") -> str | None:
        if espn and espn in self.by_espn:
            return self.by_espn[espn]
        if gsis and gsis in self.by_gsis:
            return self.by_gsis[gsis]
        cands = self.by_name.get((name_key(name), norm_pos(pos)), [])
        if not cands:
            return None
        pick = None
        if len(cands) == 1:
            pick = cands[0][0]
        else:
            same = [c for c in cands if team and c[1] == team] or [c for c in cands if c[2]]
            if len(same) == 1:
                pick = same[0][0]
        if pick:
            self.via_name += 1
        return pick


# ---------------------------------------------------------------- sleeper
def sleeper_state() -> dict:
    return get_json("https://api.sleeper.app/v1/state/nfl")


def sleeper_players() -> dict:
    return get_json("https://api.sleeper.app/v1/players/nfl")


def sleeper_projections(season: str, week: int) -> dict:
    q = "&".join(f"position[]={p}" for p in ("QB", "RB", "WR", "TE", "K", "DEF", "DL", "LB", "DB"))
    rows = get_json(f"https://api.sleeper.app/projections/nfl/{season}/{week}?season_type=regular&{q}")
    out = {}
    for r in rows or []:
        pid, st = r.get("player_id"), r.get("stats") or {}
        if not pid or not st:
            continue
        # Drop Sleeper's precomputed point totals and ADP fields; the app scores raw stats itself.
        st = {k: v for k, v in st.items() if not k.startswith(("pts_", "adp_", "pos_adp", "rank_", "pos_rank"))}
        if st:
            out[pid] = clean(st)
    return out


# ---------------------------------------------------------------- espn
ESPN_TO_SLEEPER = {
    "0": "pass_att", "1": "pass_cmp", "3": "pass_yd", "4": "pass_td", "19": "pass_2pt", "20": "pass_int",
    "23": "rush_att", "24": "rush_yd", "25": "rush_td", "26": "rush_2pt",
    "53": "rec", "42": "rec_yd", "43": "rec_td", "44": "rec_2pt", "58": "rec_tgt",
    "68": "fum", "72": "fum_lost",
}


def espn_projections(season: str, week: int, matcher: "Matcher") -> tuple[dict, int]:
    """ESPN weekly projections for QB/RB/WR/TE, keyed by Sleeper id. Returns (projections, unmatched)."""
    flt = {"players": {
        "filterSlotIds": {"value": [0, 2, 4, 6]},
        "filterStatsForTopScoringPeriodIds": {"value": 1, "additionalValue": [f"11{season}{week}"]},
        "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        "limit": 1200,
    }}
    url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}"
           f"/segments/0/leaguedefaults/3?scoringPeriodId={week}&view=kona_player_info")
    data = get_json(url, {"x-fantasy-filter": json.dumps(flt)})
    out, unmatched = {}, 0
    for entry in data.get("players", []):
        p = entry.get("player") or {}
        sid = matcher.find(p.get("fullName") or "", ESPN_POS.get(p.get("defaultPositionId"), ""),
                           ESPN_TEAM.get(p.get("proTeamId"), ""), espn=str(p.get("id") or ""))
        proj = next((s for s in p.get("stats") or []
                     if s.get("statSourceId") == 1 and s.get("statSplitTypeId") == 1
                     and s.get("scoringPeriodId") == week), None)
        if not proj or not proj.get("stats"):
            continue
        if not sid:
            unmatched += 1
            continue
        st = {ESPN_TO_SLEEPER[k]: num(v) for k, v in proj["stats"].items() if k in ESPN_TO_SLEEPER}
        if st:
            out[sid] = clean(st)
    return out, unmatched


# ---------------------------------------------------------------- nflverse model
NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download"


def recency_weights(n: int, decay: float = 0.85) -> list[float]:
    """Weights for n games ordered oldest→newest; the newest game counts most."""
    return [decay ** (n - 1 - i) for i in range(n)]


def wavg(values: list[float], weights: list[float]) -> float:
    tw = sum(weights)
    return sum(v * w for v, w in zip(values, weights)) / tw if tw else 0.0


def shrink(observed: float, n: float, prior: float, k: float) -> float:
    """Regress a rate toward a prior; k is how many attempts it takes to trust the player half way."""
    return (observed * n + prior * k) / (n + k) if (n + k) else prior


def build_model(weekly: list[dict], games: list[dict], season: str, week: int) -> dict:
    """Project a stat line (Sleeper keys) for every player with games this season. Keyed by gsis id."""
    rows = [r for r in weekly if r.get("season") == str(season) and r.get("season_type") == "REG"
            and int(r.get("week") or 0) < week]
    if not rows:
        return {}

    # Opponent for this week, and bye teams.
    opp = {}
    for g in games:
        if g.get("season") == str(season) and g.get("game_type") == "REG" and int(g.get("week") or 0) == week:
            a, h = norm_team(g["away_team"]), norm_team(g["home_team"])
            opp[a], opp[h] = h, a

    by_player = defaultdict(list)
    for r in rows:
        by_player[r["player_id"]].append(r)
    for v in by_player.values():
        v.sort(key=lambda r: int(r["week"]))

    # League-wide priors per position (simple totals across all player-games).
    tot = defaultdict(lambda: defaultdict(float))
    for r in rows:
        pos = norm_pos(r.get("position") or "")
        t = tot[pos]
        for c in ("attempts", "completions", "passing_yards", "passing_tds", "passing_interceptions",
                  "carries", "rushing_yards", "rushing_tds", "targets", "receptions", "receiving_yards",
                  "receiving_tds", "rushing_fumbles_lost", "receiving_fumbles_lost", "sack_fumbles_lost"):
            t[c] += num(r.get(c))
        t["games"] += 1

    def rate(pos, a, b, default):
        t = tot[pos]
        return t[a] / t[b] if t[b] else default

    # Opponent factor: PPR points allowed to each position vs. league average, regressed toward 1.
    allowed = defaultdict(lambda: defaultdict(float))
    team_games = defaultdict(set)
    for r in rows:
        pos = norm_pos(r.get("position") or "")
        if pos in OFFENSE and r.get("opponent_team"):
            o = norm_team(r["opponent_team"])
            allowed[o][pos] += num(r.get("fantasy_points_ppr"))
            team_games[o].add(r["week"])
    opp_factor = {}
    for pos in OFFENSE:
        per_team = {t: allowed[t][pos] / len(team_games[t]) for t in allowed if team_games[t]}
        avg = sum(per_team.values()) / len(per_team) if per_team else 0
        for t, v in per_team.items():
            g = len(team_games[t])
            raw = v / avg if avg else 1.0
            opp_factor[(t, pos)] = 1 + (raw - 1) * (g / (g + 6))  # 6 games ≈ half trust

    out = {}
    for gsis, games_list in by_player.items():
        last = games_list[-1]
        pos = norm_pos(last.get("position") or "")
        team = norm_team(last.get("team") or "")
        if team not in opp:  # bye week or team unknown
            continue
        w = recency_weights(len(games_list))
        col = lambda c: wavg([num(g.get(c)) for g in games_list], w)  # noqa: E731
        n_g = len(games_list)
        st = {}
        if pos in OFFENSE:
            of = opp_factor.get((opp[team], pos), 1.0)
            # volume per game (recency weighted)
            att, car, tgt = col("attempts"), col("carries"), col("targets")
            # efficiency, regressed to position averages
            s_att = sum(num(g.get("attempts")) for g in games_list)
            s_car = sum(num(g.get("carries")) for g in games_list)
            s_tgt = sum(num(g.get("targets")) for g in games_list)
            s = lambda c: sum(num(g.get(c)) for g in games_list)  # noqa: E731
            if pos == "QB" and att >= 5:
                ypa = shrink(s("passing_yards") / s_att if s_att else 0, s_att, rate("QB", "passing_yards", "attempts", 6.8), 150)
                tdr = shrink(s("passing_tds") / s_att if s_att else 0, s_att, rate("QB", "passing_tds", "attempts", 0.045), 300)
                intr = shrink(s("passing_interceptions") / s_att if s_att else 0, s_att, rate("QB", "passing_interceptions", "attempts", 0.024), 300)
                cmpr = shrink(s("completions") / s_att if s_att else 0, s_att, rate("QB", "completions", "attempts", 0.64), 150)
                st.update(pass_att=att, pass_cmp=att * cmpr, pass_yd=att * ypa * of,
                          pass_td=att * tdr * of, pass_int=att * intr)
            if car > 0.2:
                ypc = shrink(s("rushing_yards") / s_car if s_car else 0, s_car, rate(pos, "rushing_yards", "carries", 4.3), 60)
                rtd = shrink(s("rushing_tds") / s_car if s_car else 0, s_car, rate(pos, "rushing_tds", "carries", 0.03), 100)
                st.update(rush_att=car, rush_yd=car * ypc * of, rush_td=car * rtd * of)
            if pos != "QB" and tgt > 0.2:
                cr = shrink(s("receptions") / s_tgt if s_tgt else 0, s_tgt, rate(pos, "receptions", "targets", 0.65), 40)
                ypr = shrink(s("receiving_yards") / s("receptions") if s("receptions") else 0, s("receptions"),
                             rate(pos, "receiving_yards", "receptions", 11), 30)
                ttd = shrink(s("receiving_tds") / s_tgt if s_tgt else 0, s_tgt, rate(pos, "receiving_tds", "targets", 0.045), 80)
                st.update(rec_tgt=tgt, rec=tgt * cr * (0.5 + 0.5 * of), rec_yd=tgt * cr * ypr * of, rec_td=tgt * ttd * of)
            touches = att + car + st.get("rec", 0)
            fl = (s("rushing_fumbles_lost") + s("receiving_fumbles_lost") + s("sack_fumbles_lost")) / n_g
            st["fum_lost"] = shrink(fl, n_g, 0.003 * touches, 8)
        elif pos in IDP:
            prior = lambda c: tot[pos][c] / tot[pos]["games"] if tot[pos]["games"] else 0  # noqa: E731
            for k, c, kk in (("idp_tkl_solo", "def_tackles_solo", 1), ("idp_tkl_ast", "def_tackle_assists", 1),
                             ("idp_tkl_loss", "def_tackles_for_loss", 3), ("idp_qb_hit", "def_qb_hits", 3),
                             ("idp_sack", "def_sacks", 4), ("idp_pass_def", "def_pass_defended", 3),
                             ("idp_int", "def_interceptions", 6), ("idp_ff", "def_fumbles_forced", 6),
                             ("idp_fum_rec", "fumble_recovery_opp", 8), ("idp_def_td", "def_tds", 10),
                             ("idp_safe", "def_safeties", 12)):
                st[k] = shrink(col(c), n_g, prior(c), kk)
        else:
            continue
        # Availability: if the player missed the team's most recent game(s), scale down.
        # Count how many of the team's latest games this player missed in a row.
        team_weeks = sorted({int(r["week"]) for r in rows if norm_team(r.get("team") or "") == team})
        my_weeks = {int(g["week"]) for g in games_list}
        missed = 0
        for wk in reversed(team_weeks):
            if wk in my_weeks:
                break
            missed += 1
        st = clean(st)
        if st:
            out[gsis] = {"stats": st, "pos": pos, "team": team, "opp": opp[team], "name": last.get("player_display_name"),
                         "missed": missed}
    return out


# ---------------------------------------------------------------- main
def main() -> int:
    state = sleeper_state()
    season = str(state.get("league_season") or state.get("season"))
    week = int(os.environ.get("GD_WEEK") or state.get("week") or 0)
    if state.get("season_type") != "regular" or not 1 <= week <= 18:
        print(f"Not regular season (type={state.get('season_type')}, week={week}); nothing to do.")
        return 0
    print(f"Collecting {season} week {week}")

    players = sleeper_players()
    matcher = Matcher(players)
    sources, errors, diag = {}, {}, {}

    try:
        sources["sleeper"] = sleeper_projections(season, week)
    except Exception as e:  # noqa: BLE001
        errors["sleeper"] = str(e)

    # nflverse ids bridge gsis <-> espn, which fills gaps in Sleeper's gsis coverage.
    try:
        for r in get_csv(f"{NFLVERSE}/players/players.csv"):
            g, e = (r.get("gsis_id") or "").strip(), (r.get("espn_id") or "").strip()
            if g and e and g not in matcher.by_gsis and e in matcher.by_espn:
                matcher.by_gsis[g] = matcher.by_espn[e]
    except Exception as e:  # noqa: BLE001
        errors["nflverse_ids"] = str(e)

    try:
        espn, unmatched = espn_projections(season, week, matcher)
        sources["espn"] = espn
        print(f"espn: {len(espn)} players ({unmatched} unmatched)")
        diag["espn_unmatched"] = unmatched
    except Exception as e:  # noqa: BLE001
        errors["espn"] = str(e)

    try:
        weekly = get_csv(f"{NFLVERSE}/stats_player/stats_player_week_{season}.csv")
        games = get_csv(f"{NFLVERSE}/schedules/games.csv")
        model = build_model(weekly, games, season, week)
        sources["model"], missed = {}, 0
        for g, v in model.items():
            sid = matcher.find(v["name"] or "", v["pos"], v["team"], gsis=g)
            # The model can't tell who is returning from injury. If a player missed the team's
            # latest game, only include him when Sleeper or ESPN also expects him to play.
            if sid and v["missed"] and not any(sources.get(x, {}).get(sid) for x in ("sleeper", "espn")):
                continue
            if sid:
                sources["model"][sid] = v["stats"]
            else:
                missed += 1
        print(f"model: {len(sources['model'])} players ({missed} unmatched)")
        diag["model_unmatched"] = missed
    except Exception as e:  # noqa: BLE001
        errors["model"] = str(e)

    for k, v in sources.items():
        print(f"{k}: {len(v)} players")
    print(f"matched by name: {matcher.via_name}")
    for k, v in errors.items():
        print(f"ERROR {k}: {v}", file=sys.stderr)
    if not sources:
        return 1

    # Merge into one record per player, with the little bit of player info the app needs.
    merged = {}
    for src, rows in sources.items():
        for pid, st in rows.items():
            p = players.get(pid) or {}
            rec = merged.setdefault(pid, {
                "n": (p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}").strip() or pid,
                "pos": norm_pos(p.get("position") or ""),
                "team": p.get("team") or "",
                "src": {},
            })
            rec["src"][src] = st

    doc = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "season": season,
        "week": week,
        "sources": {
            "sleeper": {"name": "Sleeper", "players": len(sources.get("sleeper", {}))},
            "espn": {"name": "ESPN", "players": len(sources.get("espn", {}))},
            "model": {"name": "Gameday model", "players": len(sources.get("model", {}))},
        },
        "errors": errors,
        "diag": {**diag, "matched_by_name": matcher.via_name},
        "players": merged,
    }
    os.makedirs(os.path.join(OUT_DIR, "history"), exist_ok=True)
    body = json.dumps(doc, separators=(",", ":"), sort_keys=True)
    with open(os.path.join(OUT_DIR, "latest.json"), "w") as f:
        f.write(body)
    with open(os.path.join(OUT_DIR, "history", f"{season}-wk{week:02d}.json"), "w") as f:
        f.write(body)
    print(f"Wrote {len(merged)} players, {len(body) // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
