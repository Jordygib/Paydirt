"""
Paydirt data pipeline.

Builds data.json for the website from free sources:
  - nflverse (via the nflreadpy package): schedules, play-by-play, rosters, teams, injuries
  - The Odds API (free plan): moneylines, spreads, totals, and a weekly pull of player props
  - National Weather Service API: kickoff forecasts for every outdoor stadium

Run locally:   python scripts/build_data.py
Environment:   ODDS_API_KEY   (optional; without it, betting lines come from nflverse)
               PROPS_DAY      (optional; weekday to pull player props, default "Sat")
               FORCE_PROPS    (optional; set to 1 to pull props on any day, uses about 5 credits per game)
               SEASON         (optional; defaults to the current NFL season)
"""
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests

OUT = os.path.join(os.path.dirname(__file__), "..", "data.json")
ODDS_KEY = os.environ.get("ODDS_API_KEY", "").strip()
PROPS_DAY = os.environ.get("PROPS_DAY", "Sat")
ET = ZoneInfo("America/New_York")
UA = {"User-Agent": "paydirt-analytics (github actions data job)"}

# Player prop markets pulled once a week on the free plan (each market costs 1 credit per game).
PROP_MARKETS = {
    "player_anytime_td": "TD",
    "player_pass_yds": "Passing yards",
    "player_rush_yds": "Rushing yards",
    "player_receptions": "Receptions",
    "player_reception_yds": "Receiving yards",
}

# Home stadium coordinates for weather lookups and stadium notes. Verify any stadium changes each season.
STADIUMS = {
    "ARI": (33.5276, -112.2626, "Retractable roof"), "ATL": (33.7554, -84.4008, "Retractable roof"),
    "BAL": (39.2780, -76.6227, "Open air"), "BUF": (42.7738, -78.7870, "Open air"),
    "CAR": (35.2258, -80.8528, "Open air"), "CHI": (41.8623, -87.6167, "Open air"),
    "CIN": (39.0954, -84.5160, "Open air"), "CLE": (41.5061, -81.6995, "Open air"),
    "DAL": (32.7473, -97.0945, "Retractable roof"), "DEN": (39.7439, -105.0201, "Open air"),
    "DET": (42.3400, -83.0456, "Dome"), "GB": (44.5013, -88.0622, "Open air"),
    "HOU": (29.6847, -95.4107, "Retractable roof"), "IND": (39.7601, -86.1639, "Retractable roof"),
    "JAX": (30.3239, -81.6373, "Open air"), "KC": (39.0489, -94.4839, "Open air"),
    "LV": (36.0909, -115.1833, "Dome"), "LAC": (33.9535, -118.3392, "Fixed roof"),
    "LA": (33.9535, -118.3392, "Fixed roof"), "MIA": (25.9580, -80.2389, "Open air"),
    "MIN": (44.9737, -93.2581, "Dome"), "NE": (42.0909, -71.2643, "Open air"),
    "NO": (29.9511, -90.0812, "Dome"), "NYG": (40.8135, -74.0745, "Open air"),
    "NYJ": (40.8135, -74.0745, "Open air"), "PHI": (39.9008, -75.1675, "Open air"),
    "PIT": (40.4468, -80.0158, "Open air"), "SF": (37.4033, -121.9694, "Open air"),
    "SEA": (47.5952, -122.3316, "Open air"), "TB": (27.9759, -82.5033, "Open air"),
    "TEN": (36.1665, -86.7713, "Open air"), "WAS": (38.9077, -76.8645, "Open air"),
}
INDOOR_ROOFS = {"Dome", "Fixed roof", "Retractable roof"}  # retractable roofs are usually closed in bad weather

# Odds API full team names -> nflverse abbreviations
ODDS_TEAMS = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL", "Buffalo Bills": "BUF",
    "Carolina Panthers": "CAR", "Chicago Bears": "CHI", "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE",
    "Dallas Cowboys": "DAL", "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX", "Kansas City Chiefs": "KC",
    "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC", "Los Angeles Rams": "LA", "Miami Dolphins": "MIA",
    "Minnesota Vikings": "MIN", "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT", "San Francisco 49ers": "SF",
    "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB", "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}


def log(*a):
    print("[paydirt]", *a, flush=True)


def norm_name(n):
    n = re.sub(r"[^a-z ]", "", str(n).lower().replace("-", " "))
    return " ".join(w for w in n.split() if w not in {"jr", "sr", "ii", "iii", "iv", "v"})


def r1(x, d=1):
    return round(float(x), d)


# ---------------------------------------------------------------- loading
def load_nflverse(season):
    import nflreadpy as nfl  # imported here so the pure functions below can be tested without it

    def pdf(x):
        return x.to_pandas() if hasattr(x, "to_pandas") else x

    seasons = [season - 1, season]
    sched = pdf(nfl.load_schedules(seasons))
    pbp = pdf(nfl.load_pbp(seasons))
    rosters = pdf(nfl.load_rosters([season]))
    teams = pdf(nfl.load_teams())
    try:
        inj = pdf(nfl.load_injuries([season]))
    except Exception as e:  # injury reports are not always published yet
        log("injuries unavailable:", e)
        inj = pd.DataFrame()
    return sched, pbp, rosters, teams, inj


# ---------------------------------------------------------------- schedule helpers
def upcoming_week(sched, season):
    reg = sched[(sched["season"] == season) & (sched["game_type"] == "REG")]
    unplayed = reg[reg["home_score"].isna()]
    if unplayed.empty:
        return None, reg.iloc[0:0]
    wk = int(unplayed["week"].min())
    return wk, reg[reg["week"] == wk].copy()


def kickoff_utc(row):
    t = row.get("gametime")
    if not isinstance(t, str) or ":" not in t:
        t = "13:00"
    dt = datetime.strptime(f"{row['gameday']} {t}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------- stat engine
def prep_pbp(pbp, season, weeks_needed=2):
    p = pbp[(pbp["season_type"] == "REG") & pbp["posteam"].notna()].copy()
    cur = p[p["season"] == season]
    played_weeks = cur["week"].nunique()
    # early in the season, lean on last season too so numbers aren't based on one game
    use = p if played_weeks < weeks_needed else cur
    return p, use, played_weeks


def bucket(y):
    if pd.isna(y):
        return "far"
    y = float(y)
    return "1-2" if y <= 2 else "3-5" if y <= 5 else "6-10" if y <= 10 else "11-20" if y <= 20 else "21-40" if y <= 40 else "far"


def td_rates(p_all):
    """League touchdown rate per carry and per target by field position: the base of expected TDs."""
    d = p_all.copy()
    d["b"] = d["yardline_100"].map(bucket)
    rush = d[d["rush_attempt"] == 1]
    tgt = d[(d["pass_attempt"] == 1) & d["receiver_player_id"].notna()]
    rr = rush.groupby("b")["rush_touchdown"].mean().to_dict()
    tr = tgt.groupby("b")["pass_touchdown"].mean().to_dict()
    return rr, tr


def player_table(use, rosters, rr, tr, season):
    d = use.copy()
    d["b"] = d["yardline_100"].map(bucket)
    rush = d[(d["rush_attempt"] == 1) & d["rusher_player_id"].notna()].copy()
    tgt = d[(d["pass_attempt"] == 1) & d["receiver_player_id"].notna()].copy()
    rush["xtd"] = rush["b"].map(rr).fillna(0)
    tgt["xtd"] = tgt["b"].map(tr).fillna(0)
    passes = d[(d["pass_attempt"] == 1) & d["passer_player_id"].notna()]

    team_games = d.groupby("posteam")["game_id"].nunique()
    team_xtd = rush.groupby("posteam")["xtd"].sum().add(tgt.groupby("posteam")["xtd"].sum(), fill_value=0)
    team_tds = rush.groupby("posteam")["rush_touchdown"].sum().add(tgt.groupby("posteam")["pass_touchdown"].sum(), fill_value=0)
    team_opp = rush.groupby("posteam").size().add(tgt.groupby("posteam").size(), fill_value=0)

    # last three games each team played, for the usage trend
    last3 = {}
    for team, g in d.groupby("posteam"):
        gids = g.drop_duplicates("game_id").sort_values(["season", "week"])["game_id"].tail(3).tolist()
        last3[team] = set(gids)

    def agg(df, idcol, cols):
        return df.groupby([idcol, "posteam"]).agg(**cols)

    ra = agg(rush, "rusher_player_id", dict(rush=("rush_attempt", "size"), rushYd=("rushing_yards", "sum"),
                                            rtd=("rush_touchdown", "sum"), rxtd=("xtd", "sum"), rg=("game_id", "nunique")))
    ta = agg(tgt, "receiver_player_id", dict(tgt=("pass_attempt", "size"), rec=("complete_pass", "sum"),
                                             recYd=("receiving_yards", "sum"), ctd=("pass_touchdown", "sum"),
                                             cxtd=("xtd", "sum"), tg=("game_id", "nunique")))
    pa = passes.groupby(["passer_player_id", "posteam"]).agg(att=("pass_attempt", "size"), passYd=("passing_yards", "sum"),
                                                             pg=("game_id", "nunique"))
    ra.index.names = ta.index.names = pa.index.names = ["id", "team"]
    t = ra.join(ta, how="outer").join(pa, how="outer").fillna(0).reset_index()

    ros = rosters.drop_duplicates("gsis_id").set_index("gsis_id")
    rows = []
    for _, r in t.iterrows():
        if r["id"] not in ros.index:
            continue
        info = ros.loc[r["id"]]
        pos = str(info.get("position", ""))
        if pos not in {"QB", "RB", "WR", "TE"}:
            continue
        cur_team = str(info.get("team", r["team"]))
        if cur_team != r["team"]:
            continue  # only count production for his current team
        games = max(r["rg"], r["tg"], r["pg"], 1)
        team = r["team"]
        opp = r["rush"] + r["tgt"]
        share_x = (r["rxtd"] + r["cxtd"]) / max(team_xtd.get(team, 0), 1e-6)
        share_a = (r["rtd"] + r["ctd"]) / max(team_tds.get(team, 0), 1)
        share = 0.75 * share_x + 0.25 * share_a  # opportunity-based, nudged by actual scoring
        # usage trend: last 3 games' share of team carries + targets vs the full sample
        g3 = last3.get(team, set())
        p3 = rush[(rush["rusher_player_id"] == r["id"]) & rush["game_id"].isin(g3)].shape[0] + \
            tgt[(tgt["receiver_player_id"] == r["id"]) & tgt["game_id"].isin(g3)].shape[0]
        t3 = rush[(rush["posteam"] == team) & rush["game_id"].isin(g3)].shape[0] + \
            tgt[(tgt["posteam"] == team) & tgt["game_id"].isin(g3)].shape[0]
        sh_all = opp / max(team_opp.get(team, 0), 1)
        sh_3 = p3 / max(t3, 1)
        trend = 0 if sh_all <= 0 else max(-2, min(2, round((sh_3 / sh_all - 1) / 0.1)))
        num = info.get("jersey_number")
        rows.append(dict(
            id=r["id"], name=str(info.get("full_name")), team=team, pos=pos,
            share=r1(share, 3), snap=int(round(sh_all * 100)),
            tgt=r1(r["tgt"] / games), rec=r1(r["rec"] / games), recYd=r1(r["recYd"] / games),
            rush=r1(r["rush"] / games), rushYd=r1(r["rushYd"] / games), td=int(r["rtd"] + r["ctd"]),
            trend=int(trend), passYd=r1(r["passYd"] / games) if pos == "QB" else 0, att=r["att"] / games,
            num=None if pd.isna(num) else int(num),
        ))
    return pd.DataFrame(rows)


def pick_players(pt, teams_playing, injuries):
    out = []
    for team in teams_playing:
        t = pt[pt["team"] == team]
        if t.empty:
            continue
        t = t[~t["id"].isin(injuries.get("out", set()))]
        qb = t[t["pos"] == "QB"].sort_values("att", ascending=False).head(1)
        rb = t[t["pos"] == "RB"].assign(k=lambda x: x["rush"] + x["tgt"]).sort_values("k", ascending=False).head(2)
        wr = t[t["pos"] == "WR"].sort_values("tgt", ascending=False).head(3)
        te = t[t["pos"] == "TE"].sort_values("tgt", ascending=False).head(1)
        out.append(pd.concat([qb, rb, wr, te]))
    return pd.concat(out) if out else pt.iloc[0:0]


def team_tables(use, sched, season, teams_df):
    d = use
    plays = d[d["play_type"].isin(["pass", "run"])]
    g = plays.groupby("posteam")
    games = g["game_id"].nunique()
    off = {}
    sc = sched[(sched["season"].isin(d["season"].unique())) & sched["home_score"].notna() & (sched["game_type"] == "REG")]
    pts = defaultdict(list)
    for _, r in sc.iterrows():
        pts[r["home_team"]].append(r["home_score"])
        pts[r["away_team"]].append(r["away_score"])
    # red zone trips and touchdowns by drive
    rz = d[d["yardline_100"] <= 20].copy()
    drive_col = "fixed_drive" if "fixed_drive" in d.columns else "drive"
    trips = rz.groupby("posteam").apply(lambda x: x[["game_id", drive_col]].drop_duplicates().shape[0])
    tdd = rz[(rz["touchdown"] == 1) & (rz["td_team"] == rz["posteam"])].groupby("posteam").apply(
        lambda x: x[["game_id", drive_col]].drop_duplicates().shape[0])
    for team in games.index:
        tp = plays[plays["posteam"] == team]
        n = len(tp)
        off[team] = [
            r1(statistics.mean(pts[team]) if pts[team] else 22.0),
            r1(tp["yards_gained"].sum() / max(n, 1)),
            int(round(100 * tdd.get(team, 0) / max(trips.get(team, 0), 1))),
            int(round(n / games[team])),
            int(round(100 * (tp["play_type"] == "pass").sum() / max(n, 1))),
        ]
    # defense
    dg = plays.groupby("defteam")
    dgames = dg["game_id"].nunique()
    pass_yds = plays[plays["play_type"] == "pass"].groupby("defteam")["yards_gained"].sum() / dgames
    rush_yds = plays[plays["play_type"] == "run"].groupby("defteam")["yards_gained"].sum() / dgames
    pr = pass_yds.rank(method="min").astype(int)
    rrk = rush_yds.rank(method="min").astype(int)
    return off, pass_yds, rush_yds, pr, rrk, dgames


def defense_vs_position(use, rosters, dgames, pr, rrk):
    pos = rosters.drop_duplicates("gsis_id").set_index("gsis_id")["position"].to_dict()
    d = use
    rtd = d[(d["rush_touchdown"] == 1) & d["rusher_player_id"].notna()].copy()
    rtd["pos"] = rtd["rusher_player_id"].map(pos)
    ctd = d[(d["pass_touchdown"] == 1) & d["receiver_player_id"].notna()].copy()
    ctd["pos"] = ctd["receiver_player_id"].map(pos)
    counts = defaultdict(lambda: defaultdict(int))
    for _, r in rtd.iterrows():
        k = "qb" if r["pos"] == "QB" else "rb" if r["pos"] == "RB" else None
        if k:
            counts[r["defteam"]][k] += 1
    for _, r in ctd.iterrows():
        k = {"RB": "rb", "WR": "wr", "TE": "te"}.get(r["pos"])
        if k:
            counts[r["defteam"]][k] += 1
    rz = d[d["yardline_100"] <= 20]
    drive_col = "fixed_drive" if "fixed_drive" in d.columns else "drive"
    trips = rz.groupby("defteam").apply(lambda x: x[["game_id", drive_col]].drop_duplicates().shape[0])
    tdd = rz[(rz["touchdown"] == 1) & (rz["td_team"] == rz["posteam"])].groupby("defteam").apply(
        lambda x: x[["game_id", drive_col]].drop_duplicates().shape[0])
    teams = list(dgames.index)
    lg = {k: sum(counts[t][k] for t in teams) / max(sum(dgames[t] for t in teams), 1) for k in ["rb", "wr", "te", "qb"]}
    out = {}
    for t in teams:
        gms = dgames[t]
        row = {}
        for k in ["rb", "wr", "te", "qb"]:
            per = counts[t][k] / max(gms, 1)
            # shrink toward league average so a two-game fluke doesn't dominate (4 games of league-average prior)
            prior = 4
            row[k] = r1((per * gms + lg[k] * prior) / ((gms + prior) * max(lg[k], 1e-6)), 2) if lg[k] > 0 else 1.0
        row["pr"] = int(pr.get(t, 16))
        row["rr"] = int(rrk.get(t, 16))
        row["rz"] = int(round(100 * tdd.get(t, 0) / max(trips.get(t, 0), 1)))
        out[t] = row
    return out


def team_colors(teams_df):
    out = {}
    for _, r in teams_df.iterrows():
        c1 = str(r.get("team_color") or "#888888")
        c2 = str(r.get("team_color2") or c1)

        def lum(h):
            h = h.lstrip("#")
            try:
                rr, gg, bb = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            except ValueError:
                return 0
            return 0.299 * rr + 0.587 * gg + 0.114 * bb
        # very dark primaries vanish on the dark site, so use the secondary when it's brighter
        c = c2 if lum(c1) < 45 and lum(c2) > lum(c1) else c1
        out[str(r["team_abbr"])] = {"n": str(r["team_name"]), "c": c if c.startswith("#") else "#" + c}
    return out


def injury_sets(inj, season, week):
    if inj is None or inj.empty:
        return {"out": set(), "q": {}}
    w = inj[(inj["season"] == season) & (inj["week"] == week)]
    status = w.set_index("gsis_id")["report_status"].astype(str).to_dict()
    out = {k for k, v in status.items() if v in {"Out", "Doubtful"}}
    q = {k: v for k, v in status.items() if v == "Questionable"}
    return {"out": out, "q": q}


# ---------------------------------------------------------------- weather (National Weather Service)
def parse_wind(s):
    nums = [int(x) for x in re.findall(r"\d+", str(s or ""))]
    return max(nums) if nums else 0


def nws_forecast(lat, lon, kick):
    pt = requests.get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}", headers=UA, timeout=20)
    pt.raise_for_status()
    props = pt.json()["properties"]
    for key in ("forecastHourly", "forecast"):
        try:
            fc = requests.get(props[key], headers=UA, timeout=20)
            fc.raise_for_status()
            for per in fc.json()["properties"]["periods"]:
                s = datetime.fromisoformat(per["startTime"]).astimezone(timezone.utc)
                e = datetime.fromisoformat(per["endTime"]).astimezone(timezone.utc)
                if s <= kick < e:
                    pop = (per.get("probabilityOfPrecipitation") or {}).get("value") or 0
                    return dict(temp=int(per["temperature"]), wind=parse_wind(per.get("windSpeed")),
                                dir=per.get("windDirection") or "", precip=int(pop), cond=per.get("shortForecast") or "")
        except Exception as e:
            log("forecast", key, "failed:", e)
    return None


def game_weather(home, neutral, kick):
    blank = dict(temp="--", wind=0, dir="", precip=0, cond="Forecast not available yet", indoor=False)
    if neutral or home not in STADIUMS:
        blank["cond"] = "Neutral site"
        return blank
    lat, lon, roof = STADIUMS[home]
    if roof in INDOOR_ROOFS:
        return dict(temp=72, wind=0, dir="", precip=0, cond="Indoors", indoor=True)
    try:
        w = nws_forecast(lat, lon, kick)
    except Exception as e:
        log("weather failed for", home, e)
        w = None
    if not w:
        return blank
    w["indoor"] = False
    return w


# ---------------------------------------------------------------- odds (The Odds API)
def odds_get(path, params):
    params = dict(params, apiKey=ODDS_KEY)
    r = requests.get(f"https://api.the-odds-api.com/v4/{path}", params=params, timeout=30)
    r.raise_for_status()
    remaining = r.headers.get("x-requests-remaining")
    log(f"odds {path}: credits remaining {remaining}")
    return r.json(), (float(remaining) if remaining not in (None, "") else None)


def game_lines():
    """Moneylines, spreads, totals for every upcoming game. Costs 3 credits per run."""
    data, remaining = odds_get("sports/americanfootball_nfl/odds",
                               dict(regions="us", markets="h2h,spreads,totals", oddsFormat="american"))
    out = {}
    for ev in data:
        h, a = ODDS_TEAMS.get(ev["home_team"]), ODDS_TEAMS.get(ev["away_team"])
        if not h or not a:
            continue
        spreads, totals, ml = [], [], {h: None, a: None}
        for bk in ev.get("bookmakers", []):
            for m in bk.get("markets", []):
                for o in m.get("outcomes", []):
                    team = ODDS_TEAMS.get(o.get("name"))
                    if m["key"] == "spreads" and team == h and o.get("point") is not None:
                        spreads.append(o["point"])
                    elif m["key"] == "totals" and o.get("name") == "Over" and o.get("point") is not None:
                        totals.append(o["point"])
                    elif m["key"] == "h2h" and team in ml:
                        best = ml[team]
                        if best is None or o["price"] > best["price"]:
                            ml[team] = {"price": o["price"], "book": bk["title"]}
        out[(a, h)] = dict(event_id=ev["id"], spread=statistics.median(spreads) if spreads else None,
                           total=statistics.median(totals) if totals else None, ml=ml,
                           books=len(ev.get("bookmakers", [])))
    return out, remaining


def player_props(event_ids, remaining):
    """Weekly pull of player props, one game at a time. About 5 credits per game."""
    lines = {}
    for eid in event_ids:
        if remaining is not None and remaining < len(PROP_MARKETS) + 5:
            log("stopping prop pulls to protect free credits")
            break
        try:
            ev, remaining = odds_get(f"sports/americanfootball_nfl/events/{eid}/odds",
                                     dict(regions="us", markets=",".join(PROP_MARKETS), oddsFormat="american"))
        except Exception as e:
            log("props failed for", eid, e)
            continue
        acc = defaultdict(lambda: {"points": [], "over": [], "yes": []})
        for bk in ev.get("bookmakers", []):
            for m in bk.get("markets", []):
                stat = PROP_MARKETS.get(m["key"])
                if not stat:
                    continue
                for o in m.get("outcomes", []):
                    who = norm_name(o.get("description") or "")
                    if not who:
                        continue
                    a = acc[(who, stat)]
                    if stat == "TD":
                        if o.get("name") in ("Yes", "Over"):
                            a["yes"].append({"price": o["price"], "book": bk["title"]})
                    elif o.get("name") == "Over" and o.get("point") is not None:
                        a["points"].append(o["point"])
                        a["over"].append({"point": o["point"], "price": o["price"], "book": bk["title"]})
        for (who, stat), a in acc.items():
            if stat == "TD" and a["yes"]:
                best = max(a["yes"], key=lambda x: x["price"])
                lines.setdefault(who, {})["TD"] = {"price": best["price"], "book": best["book"]}
            elif a["points"]:
                line = Counter(a["points"]).most_common(1)[0][0]
                offers = [x for x in a["over"] if x["point"] == line]
                best = max(offers, key=lambda x: x["price"])
                lines.setdefault(who, {})[stat] = {"line": line, "price": best["price"], "book": best["book"],
                                                   "books": len(offers)}
    return lines


# ---------------------------------------------------------------- main
def main():
    now = datetime.now(timezone.utc)
    season = int(os.environ.get("SEASON") or (now.year if now.month >= 8 else now.year - 1))
    log("season", season)
    sched, pbp, rosters, teams_df, inj = load_nflverse(season)
    week, games = upcoming_week(sched, season)
    if week is None:
        log("no upcoming regular-season games; leaving data.json unchanged")
        return
    log("building week", week, "with", len(games), "games")

    p_all, use, played = prep_pbp(pbp, season)
    rr, tr = td_rates(p_all)
    pt = player_table(use, rosters, rr, tr, season)
    off, _, _, pr, rrk, dgames = team_tables(use, sched, season, teams_df)
    dvp = defense_vs_position(use, rosters, dgames, pr, rrk)
    colors = team_colors(teams_df)
    injuries = injury_sets(inj, season, week)

    # betting lines: The Odds API when a key is set, otherwise nflverse's lines
    lines, remaining = {}, None
    if ODDS_KEY:
        try:
            lines, remaining = game_lines()
        except Exception as e:
            log("game lines failed, falling back to nflverse:", e)

    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT))
        except Exception:
            prev = {}
    props = {}
    if prev.get("meta", {}).get("week") == week and prev.get("meta", {}).get("season") == season:
        props = prev.get("PROPLINES", {})  # carry forward this week's props between weekly pulls
    pull_props = now.astimezone(ET).strftime("%a") == PROPS_DAY or os.environ.get("FORCE_PROPS") == "1"
    if ODDS_KEY and lines and pull_props:
        upcoming_ids = []
        for _, g in games.iterrows():
            ln = lines.get((g["away_team"], g["home_team"]))
            if ln and kickoff_utc(g) > now:
                upcoming_ids.append(ln["event_id"])
        props = player_props(upcoming_ids, remaining) or props

    out_games, teams_playing = [], []
    for _, g in games.iterrows():
        a, h = g["away_team"], g["home_team"]
        kick = kickoff_utc(g)
        ln = lines.get((a, h), {})
        spread = ln.get("spread")
        total = ln.get("total")
        if spread is None and not pd.isna(g.get("spread_line")):
            spread = -float(g["spread_line"])  # nflverse: positive spread_line = home favored
        if total is None and not pd.isna(g.get("total_line")):
            total = float(g["total_line"])
        neutral = str(g.get("location", "Home")) == "Neutral"
        roof = STADIUMS.get(h, (0, 0, "Open air"))[2]
        surface = str(g.get("surface") or "").replace("_", " ").title() or "Unknown"
        if "turf" in surface.lower() or "field" in surface.lower() or "matrix" in surface.lower():
            surface = "Turf"
        elif "grass" in surface.lower():
            surface = "Grass"
        out_games.append(dict(
            id=f"{a}-{h}", away=a, home=h,
            day=kick.astimezone(ET).strftime("%a, %b %-d"), time=kick.astimezone(ET).strftime("%-I:%M %p ET"),
            kick=kick.isoformat(), spread=spread if spread is not None else 0.0, total=total if total is not None else 44.5,
            ml=ln.get("ml"), books=ln.get("books", 0),
            st=dict(name=str(g.get("stadium") or "Stadium"), city="", roof=roof, surface=surface,
                    note="Neutral-site game." if neutral else ""),
            wx=game_weather(h, neutral, kick),
        ))
        teams_playing += [a, h]

    picked = pick_players(pt, teams_playing, injuries)
    players, nums = [], {}
    for _, r in picked.iterrows():
        p = {k: r[k] for k in ["name", "team", "pos", "share", "snap", "tgt", "rec", "recYd", "rush", "rushYd", "td",
                               "trend", "passYd"]}
        p = {k: (v.item() if hasattr(v, "item") else v) for k, v in p.items()}
        if r["id"] in injuries["q"]:
            p["inj"] = "Q"
        players.append(p)
        if r.get("num") is not None and not pd.isna(r.get("num")):
            nums[p["name"]] = int(r["num"])

    # keep only prop lines for players on this week's board, keyed by display name
    keep = {norm_name(p["name"]): p["name"] for p in players}
    prop_out = {}
    for k, v in props.items():
        if k in keep:                      # fresh pull: keyed by normalized name
            prop_out[keep[k]] = v
        elif k in keep.values():           # carried forward: already keyed by display name
            prop_out[k] = v

    data = dict(
        meta=dict(season=season, week=int(week), updated=now.isoformat(), weeksPlayed=int(played),
                  oddsSource="The Odds API" if lines else "nflverse", propsSource="The Odds API" if prop_out else None),
        TEAMS=colors, OFF={k: v for k, v in off.items()}, DEF=dvp, GAMES=out_games, P=players, NUM=nums,
        PROPLINES=prop_out,
    )
    with open(OUT, "w") as f:
        json.dump(data, f, separators=(",", ":"), default=float)
    log(f"wrote {OUT}: {len(out_games)} games, {len(players)} players, {len(prop_out)} players with prop lines")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("FAILED:", repr(e))
        sys.exit(1)
