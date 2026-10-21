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
        self.players = players
        self.by_espn, self.by_gsis, self.by_name = {}, {}, defaultdict(list)
        # Sleeper sometimes keeps a stale duplicate record; let active, rostered records win id lookups.
        live = lambda p: (bool(p.get("active")), bool(p.get("team")))  # noqa: E731
        for pid, p in sorted(players.items(), key=lambda kv: live(kv[1])):
            if p.get("espn_id"):
                self.by_espn[str(p["espn_id"])] = pid
            if p.get("gsis_id"):
                self.by_gsis[str(p["gsis_id"]).strip()] = pid
            pos = norm_pos(p.get("position") or "")
            nm = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}"
            if pos and nm.strip():
                self.by_name[(name_key(nm), pos)].append((pid, p.get("team") or "", bool(p.get("active"))))
        self.by_name_all = defaultdict(dict)
        for (k, ps), lst in self.by_name.items():
            self.by_name_all[k][(k, ps)] = lst
        self.via_name = 0

    def find(self, name: str, pos: str, team: str = "", espn: str = "", gsis: str = "") -> str | None:
        id_hit = (self.by_espn.get(espn) if espn else None) or (self.by_gsis.get(gsis) if gsis else None)
        if id_hit:
            rec = self.players.get(id_hit) or {}
            if rec.get("active") and rec.get("team"):
                return id_hit
        key = name_key(name)
        pos = norm_pos(pos)
        # Every Sleeper record with this name, any position (sites disagree on DE vs LB, etc.).
        allc = [(pid, t, act, ps) for (_, ps), lst in self.by_name_all.get(key, {}).items() for pid, t, act in lst]
        pick = None
        for tier in (
            [c for c in allc if team and c[1] == team and c[3] == pos],   # same team and position
            [c for c in allc if team and c[1] == team],                    # same team
            [c for c in allc if c[3] == pos and c[2] and c[1]],            # active, on a team, same position
            [c for c in allc if c[3] == pos],                              # same position
        ):
            if len(tier) == 1:
                pick = tier[0][0]
                break
            if len(tier) > 1:
                break  # ambiguous: don't guess
        if pick and pick != id_hit:
            self.via_name += 1
        return pick or id_hit


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


# ---------------------------------------------------------------- nflverse data
NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download"

# nflverse weekly columns -> Sleeper stat keys (used for actual results and the model).
ACTUAL_MAP = {
    "pass_att": "attempts", "pass_cmp": "completions", "pass_yd": "passing_yards", "pass_td": "passing_tds",
    "pass_int": "passing_interceptions", "pass_2pt": "passing_2pt_conversions",
    "rush_att": "carries", "rush_yd": "rushing_yards", "rush_td": "rushing_tds", "rush_2pt": "rushing_2pt_conversions",
    "rec": "receptions", "rec_tgt": "targets", "rec_yd": "receiving_yards", "rec_td": "receiving_tds",
    "rec_2pt": "receiving_2pt_conversions",
    "idp_tkl_solo": "def_tackles_solo", "idp_tkl_ast": "def_tackle_assists", "idp_tkl_loss": "def_tackles_for_loss",
    "idp_qb_hit": "def_qb_hits", "idp_sack": "def_sacks", "idp_pass_def": "def_pass_defended",
    "idp_int": "def_interceptions", "idp_ff": "def_fumbles_forced", "idp_fum_rec": "fumble_recovery_opp",
    "idp_def_td": "def_tds", "idp_safe": "def_safeties",
}
IDP_STATS = [k for k in ACTUAL_MAP if k.startswith("idp_")]
# Snaps it takes to trust a defender's per-snap rate half way (rare events need far more).
IDP_K = {"idp_tkl_solo": 150, "idp_tkl_ast": 150, "idp_tkl_loss": 400, "idp_qb_hit": 300, "idp_sack": 500,
         "idp_pass_def": 400, "idp_int": 900, "idp_ff": 900, "idp_fum_rec": 1200, "idp_def_td": 2500, "idp_safe": 4000}


def actual_stats(r: dict) -> dict:
    st = {k: num(r.get(c)) for k, c in ACTUAL_MAP.items()}
    st["fum_lost"] = num(r.get("rushing_fumbles_lost")) + num(r.get("receiving_fumbles_lost")) + num(r.get("sack_fumbles_lost"))
    st["idp_tkl"] = st["idp_tkl_solo"] + st["idp_tkl_ast"]
    return clean(st)


def score(stats: dict, scoring: dict) -> float:
    return sum(v * scoring.get(k, 0) for k, v in (stats or {}).items())


def recency_weights(n: int, decay: float = 0.85) -> list[float]:
    """Weights for n games ordered oldest→newest; the newest game counts most."""
    return [decay ** (n - 1 - i) for i in range(n)]


def wavg(values: list[float], weights: list[float]) -> float:
    tw = sum(weights)
    return sum(v * w for v, w in zip(values, weights)) / tw if tw else 0.0


def shrink(observed: float, n: float, prior: float, k: float) -> float:
    """Regress a rate toward a prior; k is how many attempts it takes to trust the player half way."""
    return (observed * n + prior * k) / (n + k) if (n + k) else prior


class Season:
    """nflverse weekly stats, snap counts and schedule for one season, indexed for quick lookups."""

    def __init__(self, season: str, weekly: list[dict], snaps: list[dict], games: list[dict], pfr_to_gsis: dict):
        self.season = str(season)
        self.weekly = [r for r in weekly if r.get("season") == self.season and r.get("season_type") == "REG"]
        for r in self.weekly:
            r["_w"] = int(r["week"])
            r["_team"] = norm_team(r.get("team") or "")
            r["_opp"] = norm_team(r.get("opponent_team") or "")
            r["_pos"] = norm_pos(r.get("position") or "")
        self.snaps = defaultdict(dict)        # gsis -> week -> (off_snaps, off_pct, def_snaps, def_pct)
        for r in snaps:
            if r.get("season") != self.season or r.get("game_type") != "REG":
                continue
            g = pfr_to_gsis.get(r.get("pfr_player_id") or "")
            if g:
                self.snaps[g][int(r["week"])] = (num(r["offense_snaps"]), num(r["offense_pct"]),
                                                 num(r["defense_snaps"]), num(r["defense_pct"]))
        self.games = [g for g in games if g.get("season") == self.season and g.get("game_type") == "REG"]
        self.opp_by_week = defaultdict(dict)  # week -> team -> opponent
        for g in self.games:
            w, a, h = int(g["week"]), norm_team(g["away_team"]), norm_team(g["home_team"])
            self.opp_by_week[w][a], self.opp_by_week[w][h] = h, a
        self.weeks_played = sorted({r["_w"] for r in self.weekly})

    def rows_before(self, week: int) -> list[dict]:
        return [r for r in self.weekly if r["_w"] < week]

    def rows_in(self, week: int) -> list[dict]:
        return [r for r in self.weekly if r["_w"] == week]


def opponent_factors(rows: list[dict]) -> dict:
    """How generous each team is as an opponent, by position, vs. league average (1.0), regressed toward 1."""
    allowed = defaultdict(lambda: defaultdict(float))   # defense team -> pos -> PPR points allowed
    games = defaultdict(set)
    tackles = defaultdict(float)                        # offense team -> tackles its plays produce for defenders
    sacks = defaultdict(float)
    off_games = defaultdict(set)
    for r in rows:
        pos, o, t = r["_pos"], r["_opp"], r["_team"]
        if not o:
            continue
        if pos in OFFENSE:
            allowed[o][pos] += num(r.get("fantasy_points_ppr"))
            games[o].add(r["_w"])
        elif pos in IDP:
            tackles[o] += num(r.get("def_tackles_solo")) + num(r.get("def_tackle_assists"))
            sacks[o] += num(r.get("def_sacks"))
            off_games[o].add(r["_w"])
    f = {}
    for pos in OFFENSE:
        per = {t: allowed[t][pos] / len(games[t]) for t in allowed if games[t]}
        avg = sum(per.values()) / len(per) if per else 0
        for t, v in per.items():
            g = len(games[t])
            f[(t, pos)] = 1 + ((v / avg if avg else 1) - 1) * (g / (g + 6))
    tk = {t: tackles[t] / len(off_games[t]) for t in off_games if off_games[t]}
    sk = {t: sacks[t] / len(off_games[t]) for t in off_games if off_games[t]}
    tavg = sum(tk.values()) / len(tk) if tk else 0
    savg = sum(sk.values()) / len(sk) if sk else 0
    for t in tk:
        g = len(off_games[t])
        f[(t, "TKL")] = 1 + ((tk[t] / tavg if tavg else 1) - 1) * (g / (g + 4))
        f[(t, "SACK")] = 1 + ((sk[t] / savg if savg else 1) - 1) * (g / (g + 6))
        for pos in IDP:  # a single number per IDP position for schedule-strength display
            f[(t, pos)] = 0.75 * f[(t, "TKL")] + 0.25 * f[(t, "SACK")]
    return f


def build_model(S: Season, week: int) -> dict:
    """Project a stat line (Sleeper keys) for every player with games before `week`. Keyed by gsis id."""
    rows = S.rows_before(week)
    if not rows:
        return {}
    opp = S.opp_by_week.get(week, {})
    of_all = opponent_factors(rows)

    by_player = defaultdict(list)
    for r in rows:
        by_player[r["player_id"]].append(r)
    for v in by_player.values():
        v.sort(key=lambda r: r["_w"])

    # League-wide priors per position.
    tot = defaultdict(lambda: defaultdict(float))
    for r in rows:
        t = tot[r["_pos"]]
        for c in ("attempts", "completions", "passing_yards", "passing_tds", "passing_interceptions",
                  "carries", "rushing_yards", "rushing_tds", "targets", "receptions", "receiving_yards",
                  "receiving_tds", *ACTUAL_MAP.values()):
            t[c] += num(r.get(c))
        t["games"] += 1
        sn = S.snaps.get(r["player_id"], {}).get(r["_w"])
        if sn and r["_pos"] in IDP:
            t["def_snaps"] += sn[2]

    def rate(pos, a, b, default):
        t = tot[pos]
        return t[a] / t[b] if t[b] else default

    team_weeks = defaultdict(set)
    for r in rows:
        team_weeks[r["_team"]].add(r["_w"])

    out = {}
    for gsis, gl in by_player.items():
        last = gl[-1]
        pos, team = last["_pos"], last["_team"]
        if team not in opp:  # bye week or unknown team
            continue
        w = recency_weights(len(gl))
        col = lambda c: wavg([num(g.get(c)) for g in gl], w)  # noqa: E731
        s = lambda c: sum(num(g.get(c)) for g in gl)  # noqa: E731
        n_g = len(gl)
        st = {}
        if pos in OFFENSE:
            of = of_all.get((opp[team], pos), 1.0)
            att, car, tgt = col("attempts"), col("carries"), col("targets")
            s_att, s_car, s_tgt, s_rec = s("attempts"), s("carries"), s("targets"), s("receptions")
            if pos == "QB" and att >= 5:
                ypa = shrink(s("passing_yards") / s_att, s_att, rate("QB", "passing_yards", "attempts", 6.8), 150)
                tdr = shrink(s("passing_tds") / s_att, s_att, rate("QB", "passing_tds", "attempts", 0.045), 300)
                intr = shrink(s("passing_interceptions") / s_att, s_att, rate("QB", "passing_interceptions", "attempts", 0.024), 300)
                cmpr = shrink(s("completions") / s_att, s_att, rate("QB", "completions", "attempts", 0.64), 150)
                st.update(pass_att=att, pass_cmp=att * cmpr, pass_yd=att * ypa * of, pass_td=att * tdr * of, pass_int=att * intr)
            if car > 0.2:
                ypc = shrink(s("rushing_yards") / s_car if s_car else 0, s_car, rate(pos, "rushing_yards", "carries", 4.3), 60)
                rtd = shrink(s("rushing_tds") / s_car if s_car else 0, s_car, rate(pos, "rushing_tds", "carries", 0.03), 100)
                st.update(rush_att=car, rush_yd=car * ypc * of, rush_td=car * rtd * of)
            if pos != "QB" and tgt > 0.2:
                cr = shrink(s_rec / s_tgt if s_tgt else 0, s_tgt, rate(pos, "receptions", "targets", 0.65), 40)
                ypr = shrink(s("receiving_yards") / s_rec if s_rec else 0, s_rec, rate(pos, "receiving_yards", "receptions", 11), 30)
                ttd = shrink(s("receiving_tds") / s_tgt if s_tgt else 0, s_tgt, rate(pos, "receiving_tds", "targets", 0.045), 80)
                st.update(rec_tgt=tgt, rec=tgt * cr * (0.5 + 0.5 * of), rec_yd=tgt * cr * ypr * of, rec_td=tgt * ttd * of)
            touches = att + car + st.get("rec", 0)
            fl = (s("rushing_fumbles_lost") + s("receiving_fumbles_lost") + s("sack_fumbles_lost")) / n_g
            st["fum_lost"] = shrink(fl, n_g, 0.003 * touches, 8)
        elif pos in IDP:
            ftk, fsk = of_all.get((opp[team], "TKL"), 1.0), of_all.get((opp[team], "SACK"), 1.0)
            snaps = S.snaps.get(gsis, {})
            played = [(g["_w"], snaps.get(g["_w"])) for g in gl]
            with_snaps = [(wk, sn) for wk, sn in played if sn and sn[2] > 0]
            if len(with_snaps) >= max(1, n_g // 2):
                # Per-snap rates (regressed to the position) times expected snaps (recent games weigh most).
                tot_snaps = sum(sn[2] for _, sn in with_snaps)
                recent = [sn[2] for _, sn in with_snaps][-3:]
                ws = [0.2, 0.3, 0.5][-len(recent):]
                exp_snaps = wavg(recent, ws)
                for k in IDP_STATS:
                    c = ACTUAL_MAP[k]
                    mine = sum(num(g.get(c)) for g in gl if snaps.get(g["_w"]) and snaps[g["_w"]][2] > 0)
                    prior = tot[pos][c] / tot[pos]["def_snaps"] if tot[pos]["def_snaps"] else 0
                    per_snap = shrink(mine / tot_snaps if tot_snaps else 0, tot_snaps, prior, IDP_K[k])
                    fac = fsk if k in ("idp_sack", "idp_qb_hit", "idp_tkl_loss") else ftk
                    st[k] = per_snap * exp_snaps * fac
            else:
                prior = lambda c: tot[pos][c] / tot[pos]["games"] if tot[pos]["games"] else 0  # noqa: E731
                for k in IDP_STATS:
                    st[k] = shrink(col(ACTUAL_MAP[k]), n_g, prior(ACTUAL_MAP[k]), max(1, IDP_K[k] / 60)) * ftk
        else:
            continue
        missed = 0
        mine_w = {g["_w"] for g in gl}
        for wk in sorted(team_weeks[team], reverse=True):
            if wk in mine_w:
                break
            missed += 1
        st = clean(st)
        if st:
            out[gsis] = {"stats": st, "pos": pos, "team": team, "opp": opp[team],
                         "name": last.get("player_display_name"), "missed": missed}
    return out


def usage_table(S: Season, upto_week: int, n: int = 4) -> dict:
    """Last n weeks of snap share, targets, carries and target share per player (gsis)."""
    rows = [r for r in S.rows_before(upto_week) if r["_pos"] in OFFENSE + IDP]
    by = defaultdict(dict)
    meta = {}
    for r in rows:
        by[r["player_id"]][r["_w"]] = r
        meta[r["player_id"]] = (r.get("player_display_name") or "", r["_pos"], r["_team"])
    weeks = [w for w in S.weeks_played if w < upto_week][-n:]
    # Each team's running-back opportunities (carries + targets) per week, for backfield share.
    rb_team = defaultdict(float)
    for r in rows:
        if r["_pos"] == "RB":
            rb_team[(r["_team"], r["_w"])] += num(r.get("carries")) + num(r.get("targets"))
    out = {}
    for g, wk in by.items():
        name, pos, team = meta[g]
        sn = S.snaps.get(g, {})
        idp = pos in IDP
        u = {"wk": weeks, "sn": [], "tg": [], "ca": [], "ts": []}
        if pos == "RB":
            u["bf"] = []
        for w in weeks:
            r = wk.get(w)
            snap = sn.get(w)
            u["sn"].append(round(snap[3 if idp else 1], 2) if snap else None)
            if not idp:
                u["tg"].append(int(num(r.get("targets"))) if r else None)
                u["ca"].append(int(num(r.get("carries"))) if r else None)
                u["ts"].append(round(num(r.get("target_share")), 3) if r and r.get("target_share") not in ("", "NA", None) else None)
                if pos == "RB":
                    tot = rb_team.get((r["_team"], w), 0) if r else 0
                    u["bf"].append(round((num(r.get("carries")) + num(r.get("targets"))) / tot, 2) if r and tot else None)
        if idp:
            del u["tg"], u["ca"], u["ts"]
        if any(x is not None for x in u["sn"]) or any(x for x in u.get("tg", []) + u.get("ca", []) if x):
            out[g] = {"u": u, "name": name, "pos": pos, "team": team}
    return out


# ---------------------------------------------------------------- one week's projections
def collect_week(season: str, week: int, matcher: Matcher, S: Season | None, want_live: bool, errors: dict, diag: dict) -> dict:
    """Sources for one week, each {sleeper_id: stat line}."""
    sources = {}
    try:
        sources["sleeper"] = sleeper_projections(season, week)
    except Exception as e:  # noqa: BLE001
        errors[f"sleeper_wk{week}"] = str(e)
    try:
        espn, unmatched = espn_projections(season, week, matcher)
        sources["espn"] = espn
        diag[f"espn_unmatched_wk{week}"] = unmatched
    except Exception as e:  # noqa: BLE001
        errors[f"espn_wk{week}"] = str(e)
    if S is not None:
        try:
            model = build_model(S, week)
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
            diag[f"model_unmatched_wk{week}"] = missed
        except Exception as e:  # noqa: BLE001
            errors[f"model_wk{week}"] = f"{type(e).__name__}: {e}"
    return {k: v for k, v in sources.items() if v}


def merge(sources: dict, players: dict) -> dict:
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
    return merged


# ---------------------------------------------------------------- grading
GROUPS = ("QB", "RB", "WR", "TE", "DL", "LB", "DB")
MIN_PROJ = {"QB": 8, "RB": 4, "WR": 4, "TE": 3, "DL": 3, "LB": 3, "DB": 3}


def grade(history: dict, S: Season, matcher: Matcher, scoring: dict) -> dict:
    """Score every saved week's projections against what happened, per source and position group.

    history: week -> merged players. Only players every available source projected for that group are
    compared, so each source is graded on the same players.
    """
    actual = {}  # week -> sleeper id -> points
    for w in history:
        pts = {}
        for r in S.rows_in(w):
            sid = matcher.find(r.get("player_display_name") or "", r["_pos"], r["_team"], gsis=r["player_id"])
            if sid:
                pts[sid] = score(actual_stats(r), scoring)
        if pts:
            actual[w] = pts
    acc = {g: defaultdict(lambda: {"abs": 0.0, "sq": 0.0, "proj": 0.0, "act": 0.0, "n": 0}) for g in GROUPS}
    weeks_by_group = defaultdict(set)
    for w, merged in history.items():
        if w not in actual:
            continue
        for g in GROUPS:
            recs = [(pid, r) for pid, r in merged.items() if r["pos"] == g]
            srcs = sorted({s for _, r in recs for s in r["src"]})
            # Use only sources that cover a reasonable share of this group this week.
            srcs = [s for s in srcs if sum(1 for _, r in recs if s in r["src"]) >= 20]
            if not srcs:
                continue
            for pid, r in recs:
                if not all(s in r["src"] for s in srcs) or pid not in actual[w]:
                    continue
                proj = {s: score(r["src"][s], scoring) for s in srcs}
                cons = sum(proj.values()) / len(proj)
                if cons < MIN_PROJ[g]:
                    continue
                a = actual[w][pid]
                for s, v in list(proj.items()) + [("consensus", cons)]:
                    e = acc[g][s]
                    e["abs"] += abs(v - a)
                    e["sq"] += (v - a) ** 2
                    e["proj"] += v
                    e["act"] += a
                    e["n"] += 1
                weeks_by_group[g].add(w)
    out = {}
    for g in GROUPS:
        if not acc[g]:
            continue
        nweeks = len(weeks_by_group[g])
        stats = {s: {"mae": round(e["abs"] / e["n"], 2), "rmse": round((e["sq"] / e["n"]) ** 0.5, 2),
                     "bias": round(e["act"] / e["proj"], 3) if e["proj"] else 1.0, "n": e["n"]}
                 for s, e in acc[g].items() if e["n"]}
        srcs = [s for s in stats if s != "consensus"]
        # Weights: inverse mean squared error, blended toward equal weights until there are enough weeks.
        inv = {s: 1 / max(stats[s]["rmse"], 0.5) ** 2 for s in srcs}
        tot = sum(inv.values())
        trust = nweeks / (nweeks + 3)
        weights = {s: round((1 - trust) / len(srcs) + trust * inv[s] / tot, 3) for s in srcs}
        # Calibration: scale each source so its average matches reality, shrunk toward 1 and capped.
        calib = {}
        for s in srcs:
            n = stats[s]["n"]
            raw = stats[s]["bias"]
            calib[s] = round(min(1.6, max(0.6, 1 + (raw - 1) * n / (n + 150))), 3)
        out[g] = {"weeks": sorted(weeks_by_group[g]), "stats": stats, "weights": weights, "calib": calib}
    return out


# ---------------------------------------------------------------- main
def load_config() -> dict:
    try:
        with open(os.path.join(ROOT, "config.json")) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def write_json(path: str, doc) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = json.dumps(doc, separators=(",", ":"), sort_keys=True)
    with open(path, "w") as f:
        f.write(body)
    return len(body)


def hist_path(season: str, week: int) -> str:
    return os.path.join(OUT_DIR, "history", f"{season}-wk{week:02d}.json")


def main() -> int:
    state = sleeper_state()
    season = str(state.get("league_season") or state.get("season"))
    week = int(os.environ.get("GD_WEEK") or state.get("week") or 0)
    if state.get("season_type") != "regular" or not 1 <= week <= 18:
        print(f"Not regular season (type={state.get('season_type')}, week={week}); nothing to do.")
        return 0
    print(f"Collecting {season} week {week}")
    cfg = load_config()
    errors, diag = {}, {}

    players = sleeper_players()
    matcher = Matcher(players)

    # nflverse: ids, weekly stats, snaps, schedule.
    S = None
    try:
        nv_players = get_csv(f"{NFLVERSE}/players/players.csv")
        pfr_to_gsis = {}
        for r in nv_players:
            g, e, pf = (r.get("gsis_id") or "").strip(), (r.get("espn_id") or "").strip(), (r.get("pfr_id") or "").strip()
            if g and e and g not in matcher.by_gsis and e in matcher.by_espn:
                matcher.by_gsis[g] = matcher.by_espn[e]
            if g and pf:
                pfr_to_gsis[pf] = g
        weekly = get_csv(f"{NFLVERSE}/stats_player/stats_player_week_{season}.csv")
        games = get_csv(f"{NFLVERSE}/schedules/games.csv")
        try:
            snaps = get_csv(f"{NFLVERSE}/snap_counts/snap_counts_{season}.csv")
        except Exception as e:  # noqa: BLE001
            snaps, errors["snaps"] = [], str(e)
        S = Season(season, weekly, snaps, games, pfr_to_gsis)
    except Exception as e:  # noqa: BLE001
        errors["nflverse"] = f"{type(e).__name__}: {e}"

    # Current week.
    sources = collect_week(season, week, matcher, S, True, errors, diag)
    if not sources:
        print("No sources returned data", errors, file=sys.stderr)
        return 1
    merged = merge(sources, players)
    write_json(hist_path(season, week), {"season": season, "week": week, "players": merged})

    # Backfill earlier weeks once, so every source can be graded from Week 1.
    for w in range(1, week):
        if not os.path.exists(hist_path(season, w)):
            srcs = collect_week(season, w, matcher, S, False, errors, diag)
            if srcs:
                write_json(hist_path(season, w), {"season": season, "week": w, "players": merge(srcs, players),
                                                   "backfilled": True})
                print(f"backfilled week {w}: " + ", ".join(f"{k} {len(v)}" for k, v in srcs.items()))

    # Grade with the league's own scoring (falls back to standard PPR + common IDP values).
    scoring = cfg.get("scoring") or {}
    lid = cfg.get("sleeper_league_id")
    if lid:
        try:
            scoring = get_json(f"https://api.sleeper.app/v1/league/{lid}").get("scoring_settings") or scoring
        except Exception as e:  # noqa: BLE001
            errors["league"] = str(e)
    if not scoring:
        scoring = {"pass_yd": 0.04, "pass_td": 4, "pass_int": -2, "rush_yd": 0.1, "rush_td": 6, "rec": 1,
                   "rec_yd": 0.1, "rec_td": 6, "fum_lost": -2, "idp_tkl_solo": 1, "idp_tkl_ast": 0.5,
                   "idp_sack": 4, "idp_int": 6, "idp_ff": 4, "idp_fum_rec": 2, "idp_pass_def": 2, "idp_tkl_loss": 2}
    accuracy = {}
    if S is not None:
        history = {}
        for w in range(1, week):
            try:
                with open(hist_path(season, w)) as f:
                    history[w] = json.load(f)["players"]
            except (FileNotFoundError, KeyError, json.JSONDecodeError):
                pass
        try:
            accuracy = grade(history, S, matcher, scoring)
        except Exception as e:  # noqa: BLE001
            errors["grading"] = f"{type(e).__name__}: {e}"
    write_json(os.path.join(OUT_DIR, "accuracy.json"),
               {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "season": season,
                "through_week": week - 1, "groups": accuracy})

    # Usage, schedule and opponent strength.
    usage, sched, oppf = {}, {}, {}
    if S is not None:
        try:
            for g, v in usage_table(S, week).items():
                sid = matcher.find(v["name"], v["pos"], v["team"], gsis=g)
                if sid:
                    usage[sid] = v["u"]
        except Exception as e:  # noqa: BLE001
            errors["usage"] = f"{type(e).__name__}: {e}"
        for w in range(week, 19):
            for t, o in S.opp_by_week.get(w, {}).items():
                sched.setdefault(t, {})[str(w)] = o
        f = opponent_factors(S.rows_before(week))
        for (t, pos), v in f.items():
            if pos in GROUPS:
                oppf.setdefault(pos, {})[t] = round(v, 3)

    doc = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "season": season,
        "week": week,
        "sources": {k: {"name": n, "players": len(sources.get(k, {}))}
                    for k, n in (("sleeper", "Sleeper"), ("espn", "ESPN"), ("model", "Gameday model"))},
        "weights": {g: v["weights"] for g, v in accuracy.items()},
        "calib": {g: v["calib"] for g, v in accuracy.items()},
        "errors": errors,
        "diag": {**diag, "matched_by_name": matcher.via_name},
        "players": merged,
        "sched": sched,
        "oppf": oppf,
    }
    size = write_json(os.path.join(OUT_DIR, "latest.json"), doc)
    write_json(os.path.join(OUT_DIR, "usage.json"), {"season": season, "week": week, "players": usage})
    for k, v in sources.items():
        print(f"{k}: {len(v)} players")
    for g, v in accuracy.items():
        print(g, "weeks", v["weeks"], {s: x["mae"] for s, x in v["stats"].items()}, "weights", v["weights"])
    for k, v in errors.items():
        print(f"ERROR {k}: {v}", file=sys.stderr)
    print(f"Wrote {len(merged)} players, {size // 1024} KB; usage for {len(usage)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
