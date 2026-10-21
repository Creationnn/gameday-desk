#!/usr/bin/env python3
"""Game day check: compare the manager's set lineup with the best one and flag problems.

Reads data/latest.json (written by collect.py) and live Sleeper league data, then writes
data/report.json and report.md (the body for the GitHub issue that notifies the manager).
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collect import OUT_DIR, ROOT, get_json, load_config, norm_pos  # noqa: E402

SLOT_ELIG = {"QB": ["QB"], "RB": ["RB"], "WR": ["WR"], "TE": ["TE"], "K": ["K"], "DEF": ["DEF"],
             "FLEX": ["RB", "WR", "TE"], "WRRB_FLEX": ["WR", "RB"], "REC_FLEX": ["WR", "TE"],
             "SUPER_FLEX": ["QB", "RB", "WR", "TE"], "DL": ["DL"], "LB": ["LB"], "DB": ["DB"],
             "IDP_FLEX": ["DL", "LB", "DB"]}
NON_STARTING = {"BN", "IR", "TAXI"}
OUT = {"Out", "IR", "Sus", "PUP", "NA", "DNR", "COV"}
APP_URL = "https://creationnn.github.io/gameday-desk/"


def score(stats: dict, scoring: dict) -> float:
    return sum(v * scoring.get(k, 0) for k, v in (stats or {}).items())


def best_lineup(ids, slots, fpos, val):
    order = sorted(range(len(slots)), key=lambda i: (len(SLOT_ELIG.get(slots[i], [])), i))
    used, out = set(), ["0"] * len(slots)
    for i in order:
        allowed = SLOT_ELIG.get(slots[i], [])
        cands = [x for x in ids if x not in used and any(p in allowed for p in fpos(x))]
        if cands:
            b = max(cands, key=val)
            used.add(b)
            out[i] = b
    return out


def ncdf(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def main() -> int:
    cfg = load_config()
    with open(os.path.join(OUT_DIR, "latest.json")) as f:
        cons = json.load(f)
    week, season = cons["week"], cons["season"]
    lid = cfg["sleeper_league_id"]
    league = get_json(f"https://api.sleeper.app/v1/league/{lid}")
    user = get_json(f"https://api.sleeper.app/v1/user/{cfg['sleeper_user']}")
    rosters = get_json(f"https://api.sleeper.app/v1/league/{lid}/rosters")
    users = {u["user_id"]: u for u in get_json(f"https://api.sleeper.app/v1/league/{lid}/users")}
    matchups = get_json(f"https://api.sleeper.app/v1/league/{lid}/matchups/{week}")
    players = get_json("https://api.sleeper.app/v1/players/nfl")
    scoring = league["scoring_settings"]
    slots = [s for s in league["roster_positions"] if s not in NON_STARTING]

    def info(pid):
        p = players.get(pid) or {}
        return p

    def fpos(pid):
        p = info(pid)
        return {norm_pos(x) for x in (p.get("fantasy_positions") or [p.get("position")]) if x}

    def name(pid):
        p = info(pid)
        return p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or pid

    playing = {t for t, wk in cons.get("sched", {}).items() if str(week) in wk}

    def proj(pid):
        rec = cons["players"].get(pid)
        if not rec:
            return 0.0
        W, C = cons.get("weights", {}).get(rec["pos"], {}), cons.get("calib", {}).get(rec["pos"], {})
        num = den = 0.0
        for s, st in rec["src"].items():
            w = W.get(s, 1)
            num += w * score(st, scoring) * C.get(s, 1)
            den += w
        return num / den if den else 0.0

    def status(pid):
        p = info(pid)
        if p.get("injury_status") in OUT:
            return p["injury_status"]
        if playing and p.get("team") and p["team"] not in playing:
            return "Bye"
        return None

    def val(pid):
        return 0.0 if status(pid) else proj(pid)

    me = next(r for r in rosters if r.get("owner_id") == user["user_id"])
    mine = [x for x in (me.get("players") or []) if x not in (me.get("reserve") or [])]
    cur = me.get("starters") or []
    best = best_lineup(mine, slots, fpos, val)
    cur_total = sum(val(x) for x in cur if x != "0")
    best_total = sum(val(x) for x in best if x != "0")

    issues = []
    for i, pid in enumerate(cur):
        slot = slots[i] if i < len(slots) else "?"
        if pid == "0":
            issues.append(f"**{slot}** is empty.")
            continue
        st = status(pid)
        inj = info(pid).get("injury_status")
        if st:
            issues.append(f"**{name(pid)}** ({slot}) is {'on bye' if st == 'Bye' else st.upper()} and will score 0.")
        elif inj in ("Doubtful", "Questionable"):
            issues.append(f"**{name(pid)}** ({slot}) is listed **{inj}**. Check before kickoff.")

    best_set, cur_set = set(best), set(cur)
    moves = []
    for i, pid in enumerate(best):
        if pid != "0" and pid not in cur_set:
            out_id = cur[i] if i < len(cur) else "0"
            if out_id in best_set:  # pick any starter that leaves the best lineup instead
                out_id = next((x for x in cur if x not in best_set), "0")
            moves.append({"start": name(pid), "bench": name(out_id) if out_id != "0" else None,
                          "gain": round(val(pid) - (val(out_id) if out_id != "0" else 0), 1)})

    # Free agents for any position where a starter can't play.
    rostered = {x for r in rosters for x in (r.get("players") or []) + (r.get("reserve") or [])}
    needs = {norm_pos(info(x).get("position") or "") for x in cur if x != "0" and status(x)}
    needs |= {SLOT_ELIG[slots[i]][0] for i, x in enumerate(cur) if x == "0" and slots[i] in SLOT_ELIG}
    pickups = []
    for pos in sorted(needs):
        fa = [pid for pid, rec in cons["players"].items() if rec["pos"] == pos and pid not in rostered and not status(pid)]
        fa.sort(key=proj, reverse=True)
        for pid in fa[:3]:
            pickups.append({"pos": pos, "name": name(pid), "team": info(pid).get("team"), "proj": round(proj(pid), 1)})

    # Matchup.
    mine_m = next((m for m in matchups if m["roster_id"] == me["roster_id"]), None)
    matchup = None
    if mine_m and mine_m.get("matchup_id") is not None:
        opp_m = next((m for m in matchups if m["matchup_id"] == mine_m["matchup_id"] and m["roster_id"] != me["roster_id"]), None)
        if opp_m:
            orost = next(r for r in rosters if r["roster_id"] == opp_m["roster_id"])
            ou = users.get(orost.get("owner_id"), {})
            a = sum(val(x) for x in cur if x != "0")
            b = sum(val(x) for x in (opp_m.get("starters") or []) if x != "0")
            sd = math.sqrt(sum((0.45 * max(val(x), 2)) ** 2 for x in cur + (opp_m.get("starters") or []) if x != "0")) or 1
            matchup = {"opponent": ou.get("metadata", {}).get("team_name") or ou.get("display_name") or "Opponent",
                       "you": round(a, 1), "them": round(b, 1), "win": round(ncdf((a - b) / sd), 2)}

    report = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "season": season, "week": week,
              "league": league["name"], "projected": round(cur_total, 1), "best": round(best_total, 1),
              "issues": issues, "moves": moves, "pickups": pickups, "matchup": matchup}
    with open(os.path.join(OUT_DIR, "report.json"), "w") as f:
        json.dump(report, f, indent=1)

    n = len(issues) + len(moves)
    title = (f"Week {week}: lineup is set" if not n else
             f"Week {week}: {n} thing{'s' if n != 1 else ''} to fix before kickoff")
    lines = [f"@{cfg.get('github_user', 'Creationnn')} here's your game day check for **{league['name']}**, week {week}.", ""]
    if matchup:
        lines += [f"**You {matchup['you']} vs {matchup['opponent']} {matchup['them']}** (projected) · win chance {round(matchup['win'] * 100)}%", ""]
    lines += [f"Your lineup projects **{report['projected']}**; the best you can set is **{report['best']}**.", ""]
    if issues:
        lines += ["### Needs attention"] + [f"- {x}" for x in issues] + [""]
    if moves:
        lines += ["### Lineup changes"] + [f"- Start **{m['start']}**" + (f", bench {m['bench']}" if m['bench'] else " in the empty slot")
                                            + f" (+{m['gain']})" for m in moves] + [""]
    if pickups:
        lines += ["### Free agents to cover the gaps"] + [f"- {p['pos']}: {p['name']} ({p['team']}), {p['proj']} projected" for p in pickups] + [""]
    if not n:
        lines += ["Nothing to change. Good luck! 🏈", ""]
    lines += [f"[Open Gameday Desk]({APP_URL}) · Sleeper doesn't let apps change lineups, so make any moves in the Sleeper app."]
    with open(os.path.join(ROOT, "report.md"), "w") as f:
        f.write("\n".join(lines))
    with open(os.path.join(ROOT, "report_title.txt"), "w") as f:
        f.write(title)
    print(title)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
