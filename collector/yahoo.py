#!/usr/bin/env python3
"""Yahoo Fantasy connection for Gameday Desk.

    python collector/yahoo.py exchange CODE  -> writes yahoo_token.json (refresh token) for the workflow to encrypt
    python collector/yahoo.py fetch          -> refreshes the token and saves the manager's Yahoo NFL leagues

Needs YAHOO_CLIENT_ID and YAHOO_CLIENT_SECRET (GitHub Actions secrets). The refresh token never
appears in logs: the workflow stores it encrypted (data/yahoo_token.enc) with the client secret.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collect import OUT_DIR, ROOT  # noqa: E402

TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
API = "https://fantasysports.yahooapis.com/fantasy/v2"
TOKEN_FILE = os.path.join(ROOT, "yahoo_token.json")   # plaintext only inside the runner, never committed
YDIR = os.path.join(OUT_DIR, "yahoo")


def _client():
    cid, sec = os.environ.get("YAHOO_CLIENT_ID", "").strip(), os.environ.get("YAHOO_CLIENT_SECRET", "").strip()
    if not cid or not sec:
        sys.exit("YAHOO_CLIENT_ID / YAHOO_CLIENT_SECRET secrets are not set.")
    return cid, sec


def _token_request(fields: dict) -> dict:
    cid, sec = _client()
    body = urllib.parse.urlencode({**fields, "redirect_uri": "oob"}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={
        "Authorization": "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode(),
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # Yahoo's error body says what's wrong (expired code, wrong redirect) without echoing secrets.
        sys.exit(f"Yahoo token request failed ({e.code}): {e.read().decode()[:300]}")


def exchange(code: str) -> None:
    tok = _token_request({"grant_type": "authorization_code", "code": code.strip()})
    if "refresh_token" not in tok:
        sys.exit("Yahoo did not return a refresh token.")
    with open(TOKEN_FILE, "w") as f:
        json.dump({"refresh_token": tok["refresh_token"]}, f)
    print("Yahoo authorization worked; token saved for encryption.")


def access_token() -> str:
    with open(TOKEN_FILE) as f:
        saved = json.load(f)
    tok = _token_request({"grant_type": "refresh_token", "refresh_token": saved["refresh_token"]})
    if tok.get("refresh_token") and tok["refresh_token"] != saved["refresh_token"]:
        with open(TOKEN_FILE, "w") as f:  # Yahoo may rotate it; the workflow re-encrypts the file
            json.dump({"refresh_token": tok["refresh_token"]}, f)
    return tok["access_token"]


def yget(path: str, token: str):
    url = f"{API}/{path}{'&' if '?' in path else '?'}format=json"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["fantasy_content"]


# Yahoo's JSON wraps lists as {"0": {...}, "1": {...}, "count": n} and records as lists of one-key dicts.
def ylist(obj, key: str) -> list:
    if not isinstance(obj, dict):
        return []
    return [obj[k][key] for k in sorted((k for k in obj if k.isdigit()), key=int) if key in obj[k]]


def yflat(rec) -> dict:
    out = {}
    if isinstance(rec, list):
        for part in rec:
            out.update(yflat(part))
    elif isinstance(rec, dict):
        out.update(rec)
    return out


def fetch() -> None:
    token = access_token()
    os.makedirs(YDIR, exist_ok=True)
    users = yget("users;use_login=1/games;game_codes=nfl/leagues", token)
    leagues = []
    for user in ylist(users.get("users", {}), "user"):
        u = yflat(user[:-1]) if isinstance(user, list) else {}
        games = yflat(user[-1]).get("games", {}) if isinstance(user, list) else {}
        for game in ylist(games, "game"):
            g = yflat(game[0]) if isinstance(game, list) else yflat(game)
            lgs = yflat(game[1]).get("leagues", {}) if isinstance(game, list) and len(game) > 1 else {}
            for lg in ylist(lgs, "league"):
                leagues.append({**yflat(lg), "game_season": g.get("season")})
    current = max((l["game_season"] for l in leagues if l.get("game_season")), default=None)
    leagues = [l for l in leagues if l.get("game_season") == current]
    summary = []
    for l in leagues:
        key = l["league_key"]
        week = l.get("current_week")
        raw = {
            "settings": yget(f"league/{key}/settings", token),
            "standings": yget(f"league/{key}/standings", token),
            "rosters": yget(f"league/{key}/teams/roster;week={week}", token),
            "scoreboard": yget(f"league/{key}/scoreboard;week={week}", token),
        }
        safe = key.replace(".", "_")
        with open(os.path.join(YDIR, f"{safe}.json"), "w") as f:
            json.dump(raw, f, separators=(",", ":"))
        summary.append({"league_key": key, "name": l.get("name"), "season": l.get("season"),
                        "current_week": week, "num_teams": l.get("num_teams"), "file": f"yahoo/{safe}.json"})
        print(f"Saved Yahoo league {l.get('name')} ({key}), week {week}")
    with open(os.path.join(YDIR, "leagues.json"), "w") as f:
        json.dump({"season": current, "leagues": summary}, f, indent=1)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "exchange":
        exchange(sys.argv[2])
    elif len(sys.argv) >= 2 and sys.argv[1] == "fetch":
        fetch()
    else:
        sys.exit(__doc__)
