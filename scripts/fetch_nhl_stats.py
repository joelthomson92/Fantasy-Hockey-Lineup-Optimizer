"""Pull NHL regular-season stats into nhl-stats.json for the lineup app.

Uses the NHL's public stats API (api.nhle.com/stats/rest). For each window -
the whole season, the last 30 days, the last 14 days and all of last season -
it asks for every player's totals over that window (one request per report).
Last season never changes, so it's downloaded once and then copied forward
from the existing nhl-stats.json on every later run (set NHL_REFRESH_LAST=1 to
download it again). A normal morning run is about nine requests. The app turns the totals into fantasy points with
your league's scoring, so changing scoring never needs a new download.

Standard library only. Run:  python scripts/fetch_nhl_stats.py [output path]
Optional env: NHL_SEASON=20262027 (default: worked out from today's date).
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request

BASE = os.environ.get("NHL_API_BASE", "https://api.nhle.com/stats/rest/en")
SKATER_COLS = ["gp", "g", "a", "pm", "pim", "ppg", "ppp", "shg", "shp", "gwg", "sog", "hit", "blk"]
GOALIE_COLS = ["gp", "gs", "w", "l", "otl", "ga", "sv", "sho"]


def eastern_today():
    # Toronto is UTC-4 in the regular season's fall/spring and UTC-5 in winter;
    # games end late, so "yesterday" in either offset is the last full day.
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)).date()


def season_id(day):
    start = day.year if day.month >= 8 else day.year - 1
    return f"{start}{start + 1}"


def get_json(url, tries=4):
    req = urllib.request.Request(url, headers={"User-Agent": "lineup-optimizer-stats/1.0", "Accept": "application/json"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # network hiccup or rate limit: back off and retry
            if attempt == tries - 1:
                raise
            print(f"  retry after error: {e}", file=sys.stderr)
            time.sleep(3 * (attempt + 1))


def report(kind, name, season, since=None):
    """All rows of one aggregated report, paging if the API caps the size."""
    exp = f"seasonId={season} and gameTypeId=2"
    if since:
        exp += f' and gameDate>="{since}"'
    rows, start = [], 0
    while True:
        q = urllib.parse.urlencode({"isAggregate": "true", "isGame": "true", "start": start,
                                    "limit": -1 if start == 0 else 100, "cayenneExp": exp})
        data = get_json(f"{BASE}/{kind}/{name}?{q}")
        page = data.get("data", [])
        rows += page
        total = data.get("total", len(rows))
        if not page or len(rows) >= total:
            return rows
        start = len(rows)


def team_of(row):
    t = row.get("teamAbbrevs") or row.get("teamAbbrev") or ""
    return t.split(",")[-1].strip()  # traded players list several; keep the last


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "nhl-stats.json"
    today = eastern_today()
    as_of = today - dt.timedelta(days=1)
    season = os.environ.get("NHL_SEASON") or season_id(as_of)
    windows = {"season": None,
               "l30": (as_of - dt.timedelta(days=29)).isoformat(),
               "l14": (as_of - dt.timedelta(days=13)).isoformat()}
    order = ["season", "l30", "l14", "last"]
    prev = f"{int(season[:4]) - 1}{season[:4]}"
    skaters, goalies = {}, {}
    # reuse last season from the previous file when we already have it
    kept = None
    if os.path.exists(out_path) and not os.environ.get("NHL_REFRESH_LAST"):
        try:
            with open(out_path, encoding="utf-8") as f:
                old = json.load(f)
            if old.get("lastSeason") == prev:
                kept = old
        except Exception:
            kept = None
    for w in order:
        if w == "last" and kept:
            n = 0
            for grp, cur in (("skaters", skaters), ("goalies", goalies)):
                for pid, rec in kept.get(grp, {}).items():
                    if len(rec) > 6 and rec[6]:
                        mine = cur.setdefault(pid, rec[:3] + [None, None, None, None])
                        mine[6] = rec[6]
                        n += 1
            print(f"last: {prev} reused from {out_path} ({n} players)")
            continue
        since = windows.get(w)
        sid = prev if w == "last" else season
        print(f"{w}: {sid} since {since or 'season start'}")
        summ = report("skater", "summary", sid, since)
        real = {r["playerId"]: r for r in report("skater", "realtime", sid, since)}
        for r in summ:
            if not r.get("playerId"):
                continue
            pid = str(r["playerId"])
            x = real.get(r["playerId"], {})
            vals = [r.get("gamesPlayed"), r.get("goals"), r.get("assists"), r.get("plusMinus"),
                    r.get("penaltyMinutes"), r.get("ppGoals"), r.get("ppPoints"), r.get("shGoals"),
                    r.get("shPoints"), r.get("gameWinningGoals"), r.get("shots"),
                    x.get("hits"), x.get("blockedShots")]
            rec = skaters.setdefault(pid, [r.get("skaterFullName", ""), team_of(r), r.get("positionCode", ""), None, None, None, None])
            rec[3 + order.index(w)] = [int(v or 0) for v in vals]
            if w in ("l30", "l14") and team_of(r):
                rec[1] = team_of(r)  # most recent window wins for the current team
        for r in report("goalie", "summary", sid, since):
            if not r.get("playerId"):
                continue
            pid = str(r["playerId"])
            vals = [r.get("gamesPlayed"), r.get("gamesStarted"), r.get("wins"), r.get("losses"),
                    r.get("otLosses"), r.get("goalsAgainst"), r.get("saves"), r.get("shutouts")]
            rec = goalies.setdefault(pid, [r.get("goalieFullName", ""), team_of(r), "G", None, None, None, None])
            rec[3 + order.index(w)] = [int(v or 0) for v in vals]
            if w in ("l30", "l14") and team_of(r):
                rec[1] = team_of(r)
        print(f"  {len(summ)} skaters so far {len(skaters)}, goalies {len(goalies)}")
    windows["last"] = prev
    doc = {"app": "nhl-stats", "format": 1, "season": season, "lastSeason": prev, "asOf": as_of.isoformat(),
           "fetchedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "windows": windows, "skaterCols": SKATER_COLS, "goalieCols": GOALIE_COLS,
           "skaters": skaters, "goalies": goalies}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    print(f"wrote {out_path}: {len(skaters)} skaters, {len(goalies)} goalies, as of {as_of}")


if __name__ == "__main__":
    main()
