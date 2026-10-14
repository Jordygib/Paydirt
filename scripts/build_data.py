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
    try:  # positions for last season's players too, for the backtest
        rosters_all = pdf(nfl.load_rosters(seasons))
    except Exception as e:
        log("prior-season rosters unavailable:", e)
        rosters_all = rosters
    teams = pdf(nfl.load_teams())
    try:
        depth = pdf(nfl.load_depth_charts([season]))
    except Exception as e:
        log("depth charts unavailable:", e)
        depth = pd.DataFrame()
    try:
        snaps = pdf(nfl.load_snap_counts([season]))
    except Exception as e:
        log("snap counts unavailable:", e)
        snaps = pd.DataFrame()
    try:
        inj = pdf(nfl.load_injuries([season]))
    except Exception as e:  # injury reports are not always published yet
        log("injuries unavailable:", e)
        inj = pd.DataFrame()
    return sched, pbp, rosters, teams, inj, depth, snaps, rosters_all


SNAP_TEAM_FIX = {"GNB": "GB", "KAN": "KC", "NWE": "NE", "NOR": "NO", "SFO": "SF", "TAM": "TB", "LVR": "LV", "LAR": "LA",
                 "RAM": "LA", "SDG": "LAC", "OAK": "LV", "STL": "LA", "HTX": "HOU", "CLT": "IND", "RAV": "BAL",
                 "CRD": "ARI", "OTI": "TEN"}
POS_GROUP = {"QB": "QB", "RB": "RB", "HB": "RB", "FB": "RB", "WR": "WR", "TE": "TE",
             "T": "OL", "OT": "OL", "G": "OL", "OG": "OL", "C": "OL", "OL": "OL",
             "DE": "DL", "DT": "DL", "NT": "DL", "DL": "DL", "EDGE": "DL",
             "LB": "LB", "ILB": "LB", "OLB": "LB", "MLB": "LB",
             "CB": "DB", "S": "DB", "SS": "DB", "FS": "DB", "DB": "DB", "SAF": "DB"}


def build_snaps(snaps, season):
    """Weekly offensive and defensive snap shares for every player on every team."""
    if snaps is None or snaps.empty:
        return None
    d = snaps.copy()
    if "game_type" in d.columns:
        d = d[d["game_type"] == "REG"]
    d = d[d["season"] == season]
    if d.empty:
        return None
    d["team"] = d["team"].astype(str).map(lambda t: SNAP_TEAM_FIX.get(t, t))
    for c in ["offense_snaps", "defense_snaps", "offense_pct", "defense_pct"]:
        d[c] = pd.to_numeric(d.get(c, 0), errors="coerce").fillna(0)
    scale = 100 if max(d["offense_pct"].max(), d["defense_pct"].max()) <= 1.5 else 1  # fractions or percents
    weeks = sorted(int(w) for w in d["week"].unique())
    played = d.groupby("team")["week"].apply(lambda x: set(int(w) for w in x)).to_dict()
    pid = "pfr_player_id" if "pfr_player_id" in d.columns else "player"
    out = {}
    for (team, _), g in d.groupby(["team", pid]):
        name = str(g["player"].iloc[0])
        group = POS_GROUP.get(str(g["position"].iloc[0]).upper())
        if not group:
            continue  # kickers, punters, long snappers
        side = "D" if g["defense_snaps"].sum() > g["offense_snaps"].sum() else "O"
        col = "defense" if side == "D" else "offense"
        if g[f"{col}_snaps"].sum() <= 0:
            continue
        by_week = {int(r["week"]): r for _, r in g.iterrows()}
        pct, cnt = [], []
        for w in weeks:
            r = by_week.get(w)
            if r is None:
                pct.append(None if w not in played.get(team, set()) else 0)
                cnt.append(None if w not in played.get(team, set()) else 0)
            else:
                pct.append(int(round(float(r[f"{col}_pct"]) * scale)))
                cnt.append(int(r[f"{col}_snaps"]))
        out.setdefault(team, []).append([name, group, side, pct, cnt])
    byes = {t: [w for w in weeks if w not in played.get(t, set())] for t in out}
    return {"weeks": weeks, "byes": byes, "teams": out}


def depth_starting_qbs(depth):
    """Latest listed QB1 per team from nflverse depth charts. Column names vary by season, so detect them."""
    if depth is None or depth.empty:
        return {}
    cols = set(depth.columns)
    pos = next((c for c in ["pos_abb", "depth_position", "position"] if c in cols), None)
    rank = next((c for c in ["pos_rank", "depth_team"] if c in cols), None)
    when = next((c for c in ["dt", "week"] if c in cols), None)
    team = "club_code" if "club_code" in cols else "team" if "team" in cols else None
    if not all([pos, rank, when, team]) or "gsis_id" not in cols:
        log("depth chart columns not recognized; using recent snaps instead")
        return {}
    d = depth[depth[pos].astype(str).str.upper() == "QB"].copy()
    d["_r"] = pd.to_numeric(d[rank], errors="coerce")
    out = {}
    for t, g in d.groupby(team):
        latest = g[g[when] == g[when].max()]
        top = latest.sort_values("_r").head(1)
        if not top.empty and pd.notna(top["gsis_id"].iloc[0]):
            out[str(t)] = top["gsis_id"].iloc[0]
    return out


def load_overrides():
    """Optional overrides.json next to index.html: {"exclude": ["Name"], "starting_qb": {"WAS": "Name"}}"""
    path = os.path.join(os.path.dirname(__file__), "..", "overrides.json")
    try:
        with open(path) as f:
            o = json.load(f)
        log("using overrides.json")
        return {"exclude": {norm_name(n) for n in o.get("exclude", [])},
                "starting_qb": {k: norm_name(v) for k, v in o.get("starting_qb", {}).items()}}
    except FileNotFoundError:
        return {"exclude": set(), "starting_qb": {}}
    except Exception as e:
        log("overrides.json could not be read:", e)
        return {"exclude": set(), "starting_qb": {}}


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
        last3[team] = gids

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
        if str(info.get("status", "ACT")).upper() in {"RES", "CUT", "RET", "RSN", "RSR", "PUP", "NON", "SUS"}:
            continue  # injured reserve, released, retired, suspended
        games = max(r["rg"], r["tg"], r["pg"], 1)
        team = r["team"]
        opp = r["rush"] + r["tgt"]
        share_x = (r["rxtd"] + r["cxtd"]) / max(team_xtd.get(team, 0), 1e-6)
        share_a = (r["rtd"] + r["ctd"]) / max(team_tds.get(team, 0), 1)
        share = 0.75 * share_x + 0.25 * share_a  # opportunity-based, nudged by actual scoring
        # pull small samples toward what's typical for the position (weaker pull as games pile up)
        base = {"QB": 0.05, "RB": 0.20, "WR": 0.12, "TE": 0.09}[pos]
        w = min(0.85, 0.55 + games * 0.03)
        share = w * share + (1 - w) * base
        # usage trend: last 3 games' share of team carries + targets vs the full sample
        g3 = last3.get(team, [])
        latest = g3[-1] if g3 else None
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
            games=int(games),
            use3=int(p3),
            att_last=int(passes[(passes["passer_player_id"] == r["id"]) & (passes["game_id"] == latest)].shape[0]) if latest else 0,
            att3=int(passes[(passes["passer_player_id"] == r["id"]) & passes["game_id"].isin(g3)].shape[0]),
        ))
    return pd.DataFrame(rows)


def short_name(n):
    parts = str(n).split()
    return f"{parts[0][0]}. {' '.join(parts[1:])}" if len(parts) > 1 else str(n)


def redistribute(t, out_ids):
    """Hand ruled-out players' workload to healthy teammates. Volume moves; each player keeps his own efficiency."""
    t = t.copy()
    t["role"] = ""
    gone = t[t["id"].isin(out_ids) & (t["use3"] > 0)]
    healthy = t[~t["id"].isin(out_ids)]
    for _, o in gone.iterrows():
        if o["pos"] == "RB":
            heirs = healthy[healthy["pos"] == "RB"].sort_values(["use3", "rush"], ascending=False).head(2)
            weights = [0.60, 0.25][:len(heirs)]
        elif o["pos"] in ("WR", "TE"):
            heirs = healthy[healthy["pos"].isin(["WR", "TE"]) & (healthy["tgt"] > 0)]
            tot = heirs["tgt"].sum()
            weights = list(0.75 * heirs["tgt"] / tot) if tot > 0 else []
        else:
            continue  # QB changes are handled by starter selection
        for (idx, h), w in zip(heirs.iterrows(), weights):
            ypc = h["rushYd"] / h["rush"] if h["rush"] > 0 else 4.2
            ypt = h["recYd"] / h["tgt"] if h["tgt"] > 0 else 7.0
            cr = h["rec"] / h["tgt"] if h["tgt"] > 0 else 0.65
            add_rush, add_tgt = w * o["rush"], w * o["tgt"]
            t.loc[idx, "rush"] = r1(h["rush"] + add_rush)
            t.loc[idx, "rushYd"] = r1(h["rushYd"] + add_rush * ypc)
            t.loc[idx, "tgt"] = r1(h["tgt"] + add_tgt)
            t.loc[idx, "rec"] = r1(h["rec"] + add_tgt * cr)
            t.loc[idx, "recYd"] = r1(h["recYd"] + add_tgt * ypt)
            t.loc[idx, "share"] = r1(h["share"] + w * o["share"], 3)
            t.loc[idx, "snap"] = int(round(h["snap"] + w * o["snap"]))
            t.loc[idx, "use3"] = int(h["use3"] + w * o["use3"])
            note = f"Expanded role, {short_name(o['name'])} out"
            t.loc[idx, "role"] = note if not t.loc[idx, "role"] else t.loc[idx, "role"] + f", {short_name(o['name'])}"
            healthy = t[~t["id"].isin(out_ids)]
    return t


BLEND_STATS = ["tgt", "rec", "recYd", "rush", "rushYd", "passYd"]


def prior_per_game(p_all, season):
    """Last season's per-game stats for every player, with the team he played for most."""
    d = p_all[p_all["season"] == season - 1]
    if d.empty:
        return {}
    rush = d[(d["rush_attempt"] == 1) & d["rusher_player_id"].notna()]
    tgt = d[(d["pass_attempt"] == 1) & d["receiver_player_id"].notna()]
    pas = d[(d["pass_attempt"] == 1) & d["passer_player_id"].notna()]
    out = defaultdict(lambda: {"games": set(), "teams": Counter(), "tgt": 0, "rec": 0, "recYd": 0.0,
                               "rush": 0, "rushYd": 0.0, "passYd": 0.0})
    for pid, team, gid, yds in zip(rush["rusher_player_id"], rush["posteam"], rush["game_id"], rush["rushing_yards"].fillna(0)):
        o = out[pid]; o["games"].add(gid); o["teams"][team] += 1; o["rush"] += 1; o["rushYd"] += yds
    for pid, team, gid, c, yds in zip(tgt["receiver_player_id"], tgt["posteam"], tgt["game_id"], tgt["complete_pass"].fillna(0),
                                       tgt["receiving_yards"].fillna(0)):
        o = out[pid]; o["games"].add(gid); o["teams"][team] += 1; o["tgt"] += 1; o["rec"] += c; o["recYd"] += yds
    for pid, team, gid, yds in zip(pas["passer_player_id"], pas["posteam"], pas["game_id"], pas["passing_yards"].fillna(0)):
        o = out[pid]; o["games"].add(gid); o["teams"][team] += 1; o["passYd"] += yds
    res = {}
    for pid, o in out.items():
        g = len(o["games"])
        res[pid] = {"games": g, "team": o["teams"].most_common(1)[0][0], **{k: o[k] / g for k in BLEND_STATS}}
    return res


def blend_prior(pt, p_all, season, played):
    """Early in the season, blend this season's per-game numbers with last season's so a few big games
    don't dominate. Weight on this season = (games this season + 1) / 10, so 4 games -> 50%, 9+ games -> 100%.
    Only for players on the same team as last season with at least 4 games last year."""
    if played < 2 or pt.empty:
        return pt  # weeks 1-2 already lean on last season through the play-by-play sample
    prior = prior_per_game(p_all, season)
    pt = pt.copy()
    pt["blend"] = 1.0
    pt["cur"] = None
    for idx, r in pt.iterrows():
        pr = prior.get(r["id"])
        w = min(1.0, (r["games"] + 1) / 10)
        if not pr or pr["games"] < 4 or pr["team"] != r["team"] or w >= 1:
            continue
        cur = {k: float(r[k]) for k in BLEND_STATS}
        for k in BLEND_STATS:
            pt.at[idx, k] = r1(w * cur[k] + (1 - w) * pr[k])
        pt.at[idx, "blend"] = round(w, 2)
        pt.at[idx, "cur"] = cur
    return pt


def pick_players(pt, teams_playing, injuries, depth_qbs, overrides):
    """Pick each team's current starters: QB from depth chart or the latest game, others by last-3-game usage.
    Players ruled out (injury report or overrides exclude list) have their workload redistributed first."""
    out = []
    pt = pt.assign(_n=pt["name"].map(norm_name))
    for team in teams_playing:
        allp = pt[pt["team"] == team]
        if allp.empty:
            continue
        out_ids = set(allp[allp["id"].isin(injuries.get("out", set())) | allp["_n"].isin(overrides["exclude"])]["id"])
        allp = redistribute(allp, out_ids)
        t = allp[~allp["id"].isin(out_ids)]
        qbs = t[t["pos"] == "QB"]
        forced = overrides["starting_qb"].get(team)
        if forced and (qbs["_n"] == forced).any():
            qb = qbs[qbs["_n"] == forced].head(1)
        elif depth_qbs.get(team) in set(qbs["id"]):
            qb = qbs[qbs["id"] == depth_qbs[team]].head(1)
        else:
            qb = qbs.sort_values(["att_last", "att3", "att"], ascending=False).head(1)
        rb = t[t["pos"] == "RB"].sort_values(["use3", "rush"], ascending=False).head(2)
        wr = t[t["pos"] == "WR"].sort_values(["use3", "tgt"], ascending=False).head(3)
        te = t[t["pos"] == "TE"].sort_values(["use3", "tgt"], ascending=False).head(1)
        picked = pd.concat([qb, rb, wr, te])
        out.append(picked[picked["use3"] + picked["att3"] > 0])
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
            # shrink toward league average so a few fluky games don't dominate (10 games of league-average prior)
            prior = 10
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



# ---------------------------------------------------------------- picks of the week (graded automatically)
MARKETS = {
    "anytime td": "td", "anytime touchdown": "td", "passing yards": "passYd", "rushing yards": "rushYd",
    "receiving yards": "recYd", "receptions": "rec", "rush + rec yards": "rrYd", "rushing + receiving yards": "rrYd",
    "passing tds": "passTD", "passing touchdowns": "passTD", "moneyline": "ml", "spread": "spread", "total": "total",
    "game total": "total",
}


def load_picks():
    path = os.path.join(os.path.dirname(__file__), "..", "picks.json")
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        log("picks.json could not be read:", e)
        return None


def american_profit(odds, units):
    odds = float(odds)
    return units * (odds / 100 if odds > 0 else 100 / abs(odds))


def to_decimal(odds):
    odds = float(odds)
    return 1 + (odds / 100 if odds > 0 else 100 / abs(odds))


def leg_game(leg, sched, season, week, name_team):
    team = leg.get("team") or name_team.get(norm_name(leg.get("player", "")))
    if not team:
        return None, None
    g = sched[(sched["season"] == season) & (sched["week"] == week) &
              ((sched["home_team"] == team) | (sched["away_team"] == team))]
    return (g.iloc[0] if not g.empty else None), team


def player_line(pbp, gid, pid):
    d = pbp[pbp["game_id"] == gid]
    rush = d[d["rusher_player_id"] == pid]
    rec = d[(d["receiver_player_id"] == pid) & (d["pass_attempt"] == 1)]
    pas = d[(d["passer_player_id"] == pid) & (d["pass_attempt"] == 1)]
    tdcol = "td_player_id" if "td_player_id" in d.columns else None
    return {
        "rushYd": float(rush["rushing_yards"].fillna(0).sum()), "recYd": float(rec["receiving_yards"].fillna(0).sum()),
        "rec": float(rec["complete_pass"].fillna(0).sum()), "passYd": float(pas["passing_yards"].fillna(0).sum()),
        "passTD": float(pas["pass_touchdown"].fillna(0).sum()),
        "td": float((d[tdcol] == pid).sum()) if tdcol else float(rush["rush_touchdown"].sum() + rec["pass_touchdown"].sum()),
        "touched": len(rush) + len(rec) + len(pas),
    }


def grade_leg(leg, ctx):
    """Returns (status, detail): status in win/loss/push/void/pending/unknown."""
    if leg.get("result"):
        return str(leg["result"]).lower(), "set manually"
    market = MARKETS.get(str(leg.get("market", "")).strip().lower())
    if not market:
        return "unknown", f"unknown market '{leg.get('market')}'"
    g, team = leg_game(leg, ctx["sched"], ctx["season"], ctx["week"], ctx["name_team"])
    if g is None:
        return "unknown", "game not found"
    if pd.isna(g.get("home_score")):
        return "pending", ""
    hs, as_ = float(g["home_score"]), float(g["away_score"])
    if market in ("ml", "spread", "total"):
        mine, theirs = (hs, as_) if team == g["home_team"] else (as_, hs)
        if market == "ml":
            return ("win" if mine > theirs else "push" if mine == theirs else "loss"), f"{int(mine)}-{int(theirs)}"
        if market == "spread":
            m = mine + float(leg.get("line", 0)) - theirs
            return ("win" if m > 0 else "push" if m == 0 else "loss"), f"{int(mine)}-{int(theirs)}"
        tot, line = hs + as_, float(leg.get("line", 0))
        side = str(leg.get("side", "over")).lower()
        res = "push" if tot == line else ("win" if (tot > line) == (side == "over") else "loss")
        return res, f"{int(tot)} total points"
    pid = ctx["name_id"].get(norm_name(leg.get("player", "")))
    if not pid:
        return "unknown", "player not found"
    st = player_line(ctx["pbp"], g["game_id"], pid)
    played = st["touched"] > 0 or norm_name(leg.get("player", "")) in ctx["snapped"].get(g["game_id"], set())
    if not played:
        return "void", "did not play"
    if market == "td":
        return ("win" if st["td"] > 0 else "loss"), f"{int(st['td'])} TD"
    val = st["rushYd"] + st["recYd"] if market == "rrYd" else st[market]
    line = float(leg.get("line", 0))
    side = str(leg.get("side", "over")).lower()
    res = "push" if val == line else ("win" if (val > line) == (side == "over") else "loss")
    unit = {"rec": "catches", "passTD": "pass TD"}.get(market, "yds")
    return res, f"{int(val) if float(val).is_integer() else round(val, 1)} {unit}"


def settle(bet, legs_graded):
    """Settle a parlay or single bet in units."""
    units = float(bet.get("units", 1))
    if bet.get("result"):
        res = str(bet["result"]).lower()
    else:
        st = [s for s, _ in legs_graded]
        if any(s == "loss" for s in st):
            res = "loss"
        elif any(s in ("pending", "unknown") for s in st):
            res = "pending"
        elif all(s in ("push", "void") for s in st):
            res = "push"
        else:
            res = "win"
    if res == "loss":
        return res, -units
    if res in ("push", "pending"):
        return res, 0.0
    # win: if any leg pushed or was voided, pay the remaining legs at their own odds when given
    st = [s for s, _ in legs_graded]
    legs = bet.get("legs") or [bet]
    if any(s in ("push", "void") for s in st) and len(legs) > 1:
        live = [l for l, (s, _) in zip(legs, legs_graded) if s == "win"]
        if all(l.get("odds") is not None for l in live):
            dec = 1.0
            for l in live:
                dec *= to_decimal(l["odds"])
            return "win", round(units * (dec - 1), 2)
        return "review", 0.0  # needs a manual result: add "result" and "profit" to this bet in picks.json
    if bet.get("profit") is not None:
        return "win", float(bet["profit"])
    return "win", round(american_profit(bet["odds"], units), 2)


def grade_picks(picks, prev, sched, pbp, rosters, snaps, season, now):
    if not picks or not picks.get("weeks"):
        return None
    ros = rosters.drop_duplicates("gsis_id")
    name_id = {norm_name(n): i for n, i in zip(ros["full_name"], ros["gsis_id"])}
    name_team = {norm_name(n): t for n, t in zip(ros["full_name"], ros["team"])}
    snapped = defaultdict(set)
    if snaps is not None and not snaps.empty and "game_id" in snaps.columns:
        for _, r in snaps[pd.to_numeric(snaps.get("offense_snaps", 0), errors="coerce").fillna(0) > 0].iterrows():
            snapped[str(r["game_id"])].add(norm_name(r["player"]))
    locked = {f"{w['season']}-{w['week']}": w for w in (((prev or {}).get("PICKS") or {}).get("weeks") or [])}
    weeks_out = []
    for wk in picks["weeks"]:
        wseason, wnum = int(wk.get("season", season)), int(wk["week"])
        ctx = dict(sched=sched, season=wseason, week=wnum, name_team=name_team, name_id=name_id, pbp=pbp, snapped=snapped)
        key = f"{wseason}-{wnum}"
        entry = {"season": wseason, "week": wnum, "posted": wk.get("posted")}
        for kind in ("parlay", "prop"):
            bet = wk.get(kind)
            if not bet:
                continue
            legs = bet.get("legs") if kind == "parlay" else [bet]
            # lock: once the first game in this bet has kicked off, keep the version that was live at kickoff
            kicks = []
            for leg in legs:
                g, _ = leg_game(leg, sched, wseason, wnum, name_team)
                if g is not None:
                    kicks.append(kickoff_utc(g))
            first_kick = min(kicks) if kicks else None
            prior = (locked.get(key) or {}).get(kind)
            if first_kick and now >= first_kick and prior and prior.get("source"):
                src = json.loads(prior["source"])
                for i, leg in enumerate(legs):  # manual results may still be added after kickoff
                    if leg.get("result") and i < len(src.get("legs", [src])):
                        (src.get("legs") or [src])[i]["result"] = leg["result"]
                for k in ("result", "profit"):
                    if bet.get(k) is not None:
                        src[k] = bet[k]
                bet = src
                legs = bet.get("legs") if kind == "parlay" else [bet]
            graded = [grade_leg(l, ctx) for l in legs]
            res, profit = settle(bet, graded)
            entry[kind] = {
                "odds": bet.get("odds"), "units": float(bet.get("units", 1)), "result": res, "profit": profit,
                "kick": first_kick.isoformat() if first_kick else None,
                "locked": bool(first_kick and now >= first_kick),
                "note": bet.get("note", ""),
                "legs": [dict(player=l.get("player"), team=l.get("team"), market=l.get("market"), side=l.get("side"),
                              line=l.get("line"), odds=l.get("odds"), status=s, detail=d) for l, (s, d) in zip(legs, graded)],
                "source": json.dumps(bet),
            }
        weeks_out.append(entry)
    weeks_out.sort(key=lambda w: (w["season"], w["week"]), reverse=True)

    def summarize(kind):
        bets = [w[kind] for w in weeks_out if kind in w and w[kind]["result"] in ("win", "loss", "push")]
        risked = sum(b["units"] for b in bets if b["result"] != "push")
        units = round(sum(b["profit"] for b in bets), 2)
        return {"w": sum(b["result"] == "win" for b in bets), "l": sum(b["result"] == "loss" for b in bets),
                "p": sum(b["result"] == "push" for b in bets), "units": units,
                "roi": round(100 * units / risked, 1) if risked else 0.0}
    legs_all = [l for w in weeks_out if "parlay" in w for l in w["parlay"]["legs"] if l["status"] in ("win", "loss")]
    return {"weeks": weeks_out, "record": {"parlay": summarize("parlay"), "prop": summarize("prop"),
            "legs": {"hit": sum(l["status"] == "win" for l in legs_all), "total": len(legs_all)}}}



# ---------------------------------------------------------------- backtest + self-correcting calibration
POS_BASE = {"QB": 0.05, "RB": 0.20, "WR": 0.12, "TE": 0.09}
CAL_FEATURES = ["const", "log_lam", "RB", "WR", "TE", "log_tgt", "log_rush"]


def backtest_rows(pbp, sched, rosters_all, rr, tr, min_week=4):
    """Re-run the TD model on every past week using only games played before it, and record who scored.
    Returns one row per player-game: the raw model inputs/output plus whether he actually scored."""
    d = pbp[(pbp["season_type"] == "REG") & pbp["posteam"].notna()].copy()
    if d.empty:
        return pd.DataFrame()
    d["b"] = d["yardline_100"].map(bucket)
    pos_of = rosters_all.drop_duplicates("gsis_id", keep="last").set_index("gsis_id")["position"].to_dict()
    rush = d[(d["rush_attempt"] == 1) & d["rusher_player_id"].notna()]
    tgt = d[(d["pass_attempt"] == 1) & d["receiver_player_id"].notna()]
    o = pd.concat([
        pd.DataFrame({"season": rush["season"], "week": rush["week"], "team": rush["posteam"], "opp": rush["defteam"],
                      "pid": rush["rusher_player_id"], "xtd": rush["b"].map(rr).fillna(0), "td": rush["rush_touchdown"],
                      "r": 1, "t": 0}),
        pd.DataFrame({"season": tgt["season"], "week": tgt["week"], "team": tgt["posteam"], "opp": tgt["defteam"],
                      "pid": tgt["receiver_player_id"], "xtd": tgt["b"].map(tr).fillna(0), "td": tgt["pass_touchdown"],
                      "r": 0, "t": 1}),
    ], ignore_index=True)
    o["pos"] = o["pid"].map(pos_of)
    o = o[o["pos"].isin(["QB", "RB", "WR", "TE"])]
    pw = o.groupby(["season", "pid", "team", "week"]).agg(xtd=("xtd", "sum"), td=("td", "sum"), r=("r", "sum"),
                                                         t=("t", "sum")).reset_index()
    tw = o.groupby(["season", "team", "week"]).agg(txtd=("xtd", "sum"), ttd=("td", "sum"), topp=("r", "size")).reset_index()
    # touchdowns allowed by each defense to each position, per week
    tdp = o[o["td"] == 1].copy()
    tdp["k"] = tdp.apply(lambda x: "qb" if x["pos"] == "QB" else x["pos"].lower(), axis=1)
    dallow = tdp.groupby(["season", "opp", "week", "k"]).size().unstack("k", fill_value=0).reset_index()
    dgames = o.groupby(["season", "opp", "week"]).size().reset_index()[["season", "opp", "week"]]
    # who scored (any touchdown) in each week
    if "td_player_id" in d.columns:
        sc = d[d["td_player_id"].notna()].groupby(["season", "week", "td_player_id"]).size()
        scored = {(s_, w_, p_) for (s_, w_, p_) in sc.index}
    else:
        scored = {(s_, w_, p_) for s_, w_, p_ in o[o["td"] == 1][["season", "week", "pid"]].itertuples(index=False)}
    sch = sched[sched["game_type"] == "REG"]
    rows = []
    for season in sorted(o["season"].unique()):
        po, tws, das = pw[pw["season"] == season], tw[tw["season"] == season], dallow[dallow["season"] == season]
        dgs = dgames[dgames["season"] == season]
        ssch = sch[sch["season"] == season]
        for w in sorted(po["week"].unique()):
            if w < min_week:
                continue
            prior, now = po[po["week"] < w], po[po["week"] == w]
            if prior.empty or now.empty:
                continue
            cum = prior.groupby(["pid", "team"]).agg(xtd=("xtd", "sum"), td=("td", "sum"), r=("r", "sum"), t=("t", "sum"),
                                                     games=("week", "nunique")).reset_index()
            tcum = tws[tws["week"] < w].groupby("team").agg(txtd=("txtd", "sum"), ttd=("ttd", "sum"), topp=("topp", "sum"))
            # defense multipliers, shrunk toward league average (10 games of prior)
            dc = das[das["week"] < w].groupby("opp").sum(numeric_only=True)
            dg = dgs[dgs["week"] < w].groupby("opp").size()
            lg = {k: (dc[k].sum() / max(dg.sum(), 1)) if k in dc.columns else 0 for k in ["rb", "wr", "te", "qb"]}
            # last three team weeks for the usage trend
            last3 = {t_: sorted(g["week"][g["week"] < w])[-3:] for t_, g in tws.groupby("team")}
            # game context for week w
            gw = ssch[ssch["week"] == w]
            ctx = {}
            for _, g in gw.iterrows():
                if pd.isna(g.get("spread_line")) or pd.isna(g.get("total_line")):
                    continue
                hs = g["total_line"] / 2 + g["spread_line"] / 2  # spread_line > 0 means home favored
                roof = str(g.get("roof") or "")
                wind = 0 if roof in ("dome", "closed") or pd.isna(g.get("wind")) else float(g["wind"])
                ctx[g["home_team"]] = (hs, g["away_team"], wind)
                ctx[g["away_team"]] = (g["total_line"] - hs, g["home_team"], wind)
            now_ids = set(zip(now["pid"], now["team"]))
            for c in cum.itertuples(index=False):
                if (c.pid, c.team) not in now_ids or c.team not in ctx or c.team not in tcum.index:
                    continue
                pos = pos_of.get(c.pid)
                tc = tcum.loc[c.team]
                sx = c.xtd / max(tc["txtd"], 1e-6)
                sa = c.td / max(tc["ttd"], 1)
                share = 0.75 * sx + 0.25 * sa
                wsh = min(0.85, 0.55 + c.games * 0.03)
                share = wsh * share + (1 - wsh) * POS_BASE[pos]
                if share < 0.03 or c.games < 2:
                    continue
                pts, opp, wind = ctx[c.team]
                k = "qb" if pos == "QB" else pos.lower()
                g_ = dg.get(opp, 0)
                per = (dc.loc[opp, k] if (opp in dc.index and k in dc.columns) else 0) / max(g_, 1)
                mult = (per * g_ + lg[k] * 10) / ((g_ + 10) * lg[k]) if lg[k] > 0 else 1.0
                dadj = 1 + (mult - 1) * 0.7
                kind = "pass" if pos in ("WR", "TE") else "rush"
                wx = 1.0
                if wind >= 20:
                    wx = 0.88 if kind == "pass" else 1.05
                elif wind >= 15:
                    wx = 0.92 if kind == "pass" else 1.03
                l3 = last3.get(c.team, [])
                pp3 = prior[(prior["pid"] == c.pid) & prior["week"].isin(l3)][["r", "t"]].sum().sum()
                tt3 = tws[(tws["team"] == c.team) & tws["week"].isin(l3)]["topp"].sum()
                sh_all = (c.r + c.t) / max(tc["topp"], 1)
                trend = 0 if sh_all <= 0 else max(-2, min(2, round(((pp3 / max(tt3, 1)) / sh_all - 1) / 0.1)))
                lam = pts * TD_PER_POINT * share * dadj * wx * (1 + trend * 0.04)
                rows.append(dict(season=int(season), week=int(w), pos=pos, lam=lam, tgt=c.t / c.games, rush=c.r / c.games,
                                 y=int((season, w, c.pid) in scored)))
    return pd.DataFrame(rows)


TD_PER_POINT = 0.105


def cal_matrix(df):
    import numpy as np
    return np.column_stack([np.ones(len(df)), np.log(np.maximum(df["lam"].values, 1e-4)),
                            (df["pos"] == "RB").values, (df["pos"] == "WR").values, (df["pos"] == "TE").values,
                            np.log1p(df["tgt"].values), np.log1p(df["rush"].values)]).astype(float)


def fit_logistic(X, y, ridge=1.0, iters=50):
    """Logistic regression by Newton's method with a light ridge penalty (no extra packages needed)."""
    import numpy as np
    b = np.zeros(X.shape[1])
    pen = np.eye(X.shape[1]) * ridge
    pen[0, 0] = 0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(X @ b)))
        W = p * (1 - p)
        H = X.T @ (X * W[:, None]) + pen
        g = X.T @ (p - y) + pen @ b
        step = np.linalg.solve(H, g)
        b -= step
        if np.abs(step).max() < 1e-7:
            break
    return b


def calibrate(bt, min_rows=1500):
    """Fit the correction layer on real outcomes, report honest out-of-sample accuracy, return coefficients."""
    import numpy as np
    if bt is None or len(bt) < min_rows:
        log("calibration skipped: not enough past player-games yet", 0 if bt is None else len(bt))
        return None
    bt = bt.sort_values(["season", "week"]).reset_index(drop=True)
    X, y = cal_matrix(bt), bt["y"].values.astype(float)
    raw = 1 - np.exp(-bt["lam"].values)
    # honest test: fit on the earlier 75% of weeks, score the latest 25%
    keys = bt[["season", "week"]].drop_duplicates().values.tolist()
    cut = keys[int(len(keys) * 0.75)]
    tr_ = ((bt["season"] < cut[0]) | ((bt["season"] == cut[0]) & (bt["week"] < cut[1]))).values
    b_tr = fit_logistic(X[tr_], y[tr_])
    te = ~tr_
    p_te = 1 / (1 + np.exp(-(X[te] @ b_tr)))

    def ll(p, yy):
        p = np.clip(p, 1e-4, 1 - 1e-4)
        return float(-np.mean(yy * np.log(p) + (1 - yy) * np.log(1 - p)))

    def brier(p, yy):
        return float(np.mean((p - yy) ** 2))
    b_all = fit_logistic(X, y)
    cal_all = 1 / (1 + np.exp(-(X @ b_all)))
    bins = []
    edges = [0, .1, .2, .3, .4, .5, .6, .7, 1.01]
    for lo, hi in zip(edges[:-1], edges[1:]):
        mr, mc = (raw >= lo) & (raw < hi), (cal_all >= lo) & (cal_all < hi)
        bins.append(dict(lo=lo, hi=min(hi, 1.0),
                         raw_pred=round(float(raw[mr].mean()), 3) if mr.sum() else None,
                         raw_act=round(float(y[mr].mean()), 3) if mr.sum() else None, raw_n=int(mr.sum()),
                         cal_pred=round(float(cal_all[mc].mean()), 3) if mc.sum() else None,
                         cal_act=round(float(y[mc].mean()), 3) if mc.sum() else None, cal_n=int(mc.sum())))
    out = dict(features=CAL_FEATURES, coef=[round(float(v), 5) for v in b_all], n=int(len(bt)),
               weeks=len(keys), test_n=int(te.sum()),
               test_brier_raw=round(brier(raw[te], y[te]), 4), test_brier_cal=round(brier(p_te, y[te]), 4),
               test_logloss_raw=round(ll(raw[te], y[te]), 4), test_logloss_cal=round(ll(p_te, y[te]), 4),
               bins=bins)
    log(f"calibration: {out['n']} player-games, test brier raw {out['test_brier_raw']} -> calibrated {out['test_brier_cal']}")
    return out


# ---------------------------------------------------------------- main
def main():
    now = datetime.now(timezone.utc)
    season = int(os.environ.get("SEASON") or (now.year if now.month >= 8 else now.year - 1))
    log("season", season)
    sched, pbp, rosters, teams_df, inj, depth, snaps, rosters_all = load_nflverse(season)
    depth_qbs = depth_starting_qbs(depth)
    overrides = load_overrides()
    week, games = upcoming_week(sched, season)
    if week is None:
        log("no upcoming regular-season games; leaving data.json unchanged")
        return
    log("building week", week, "with", len(games), "games")

    p_all, use, played = prep_pbp(pbp, season)
    rr, tr = td_rates(p_all)
    pt = player_table(use, rosters, rr, tr, season)
    try:
        calib = calibrate(backtest_rows(p_all, sched, rosters_all, rr, tr))
    except Exception as e:  # never let calibration take down the daily update
        log("calibration failed:", repr(e))
        calib = None
    pt = blend_prior(pt, p_all, season, played)
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

    picked = pick_players(pt, teams_playing, injuries, depth_qbs, overrides)
    players, nums = [], {}
    for _, r in picked.iterrows():
        p = {k: r[k] for k in ["name", "team", "pos", "share", "snap", "tgt", "rec", "recYd", "rush", "rushYd", "td",
                               "trend", "passYd"]}
        p = {k: (v.item() if hasattr(v, "item") else v) for k, v in p.items()}
        if r["id"] in injuries["q"]:
            p["inj"] = "Q"
        if r.get("role"):
            p["role"] = r["role"]
        if r.get("blend") is not None and r.get("blend") < 1 and isinstance(r.get("cur"), dict):
            p["blend"] = float(r["blend"])
            p["cur"] = {k: r1(v) for k, v in r["cur"].items()}
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
        SNAPS=build_snaps(snaps, season),
        CALIB=calib,
        PICKS=grade_picks(load_picks(), prev, sched, pbp, rosters, snaps, season, now),
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
