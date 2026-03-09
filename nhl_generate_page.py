"""
NHL STATS BOT - Generateur de page GitHub Pages avec historique
===============================================================
Chaque matin a 10h :
1. Verifie les resultats reels de la veille (points marques ?)
2. Sauvegarde le top 20 du jour dans history.json
3. Regenere index.html avec onglet historique + taux de reussite global
"""

import requests
import pandas as pd
from scipy.stats import poisson
import numpy as np
from datetime import datetime, timedelta
from itertools import combinations
import time
import os
import json
import base64

# ============================================================
# CONFIGURATION
# ============================================================

LINE          = 0.5
SEASON        = "20252026"
LAST_N_GAMES  = 27
RETRY_MAX     = 3
RETRY_DELAY   = 3
TOP_N         = 20

# --- Filtres modele V7 ---
MIN_TOI_FORWARD    = 12.0
MIN_TOI_DEFENSEMAN = 18.0
MIN_SCORE_RATE     = 0.25
MIN_PROB_HISTORY   = 65    # seuil pour sauvegarder dans l historique

# --- Gardiens ---
GOALIE_BACKUP_BONUS = 0.06
GOALIE_ELITE_MALUS  = 0.06
ELITE_GOALIES = {
    "Connor Hellebuyck", "Andrei Vasilevskiy", "Igor Shesterkin",
    "Juuse Saros", "Linus Ullmark", "Jake Oettinger", "Adin Hill",
    "Jeremy Swayman", "Thatcher Demko", "Samuel Montembeault",
}

# --- The Odds API ---
ODDS_API_KEY  = "f9d5e5c7a85eddd7fad39617a5c163fe"
VALUE_MIN_PCT = 5.0
COTE_DEFAUT   = 1.27

GITHUB_TOKEN  = os.environ.get("GH_TOKEN", "")
GITHUB_REPO   = "Thiboulard/NHL-Stats"
GITHUB_BRANCH = "main"
HISTORY_FILE  = "history.json"
HTML_FILE     = "index.html"

# ============================================================
# GITHUB HELPERS
# ============================================================

def github_get(path):
    """Recupere un fichier depuis GitHub. Retourne (content_str, sha) ou (None, None)."""
    url = "https://api.github.com/repos/{}/contents/{}".format(GITHUB_REPO, path)
    headers = {
        "Authorization": "token {}".format(GITHUB_TOKEN),
        "Accept": "application/vnd.github.v3+json",
    }
    r = requests.get(url, headers=headers)
    if r.status_code == 200:
        data    = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, data["sha"]
    return None, None


def github_put(path, content_str, message, sha=None):
    """Cree ou met a jour un fichier sur GitHub."""
    url = "https://api.github.com/repos/{}/contents/{}".format(GITHUB_REPO, path)
    headers = {
        "Authorization": "token {}".format(GITHUB_TOKEN),
        "Accept": "application/vnd.github.v3+json",
    }
    payload = {
        "message": message,
        "content": base64.b64encode(content_str.encode("utf-8")).decode("utf-8"),
        "branch":  GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(url, headers=headers, json=payload)
    return r.status_code in (200, 201)


def load_history():
    """Charge l historique depuis GitHub. Retourne (dict, sha)."""
    content, sha = github_get(HISTORY_FILE)
    if content:
        try:
            return json.loads(content), sha
        except Exception:
            pass
    return {"days": []}, None


def save_history(history, sha=None):
    """Sauvegarde l historique sur GitHub."""
    content = json.dumps(history, ensure_ascii=False, indent=2)
    msg = "auto: update history - {}".format(datetime.now().strftime("%Y-%m-%d"))
    ok = github_put(HISTORY_FILE, content, msg, sha)
    if ok:
        print("Historique sauvegarde sur GitHub")
    else:
        print("Erreur sauvegarde historique")
    return ok


# ============================================================
# NHL API
# ============================================================

def fetch_todays_games():
    today = datetime.now().strftime("%Y-%m-%d")
    url   = "https://api-web.nhle.com/v1/schedule/{}".format(today)
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data, games = r.json(), []
        for day in data.get("gameWeek", []):
            if day.get("date") != today:
                continue
            for g in day.get("games", []):
                games.append({
                    "game_id": g.get("id"),
                    "home":    g.get("homeTeam", {}).get("abbrev", "???"),
                    "away":    g.get("awayTeam", {}).get("abbrev", "???"),
                    "time":    g.get("startTimeUTC", ""),
                })
        print(">>> {} match(s) trouves ({})".format(len(games), today))
        return games
    except Exception as e:
        print("Erreur matchs du jour : {}".format(e))
        return []


def fetch_roster_by_team_abbrev(team_abbrev):
    url = "https://api-web.nhle.com/v1/roster/{}/{}".format(team_abbrev, SEASON)
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data, players = r.json(), []
        for group in ["forwards", "defensemen"]:
            for p in data.get(group, []):
                fn = p.get("firstName", {}).get("default", "")
                ln = p.get("lastName",  {}).get("default", "")
                pos = "D" if group == "defensemen" else "F"
                players.append({
                    "id":   p.get("id"),
                    "name": "{} {}".format(fn, ln).strip(),
                    "pos":  pos,
                    "team": team_abbrev,
                })
        return players
    except Exception as e:
        print("  Erreur roster {} : {}".format(team_abbrev, e))
        return []


def _toi_to_minutes(toi_str):
    try:
        parts = toi_str.split(":")
        return round(int(parts[0]) + (int(parts[1]) if len(parts) > 1 else 0) / 60, 4)
    except (ValueError, IndexError):
        return 0.0


def fetch_game_logs(player_id):
    url = "https://api-web.nhle.com/v1/player/{}/game-log/{}/2".format(player_id, SEASON)
    for attempt in range(1, RETRY_MAX + 1):
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            logs_raw = r.json().get("gameLog", [])[:LAST_N_GAMES]
            logs = []
            for game in logs_raw:
                goals   = game.get("goals",   0)
                assists = game.get("assists", 0)
                toi_min = _toi_to_minutes(game.get("toi", "0:00"))
                logs.append({
                    "date":    game.get("gameDate", "N/A"),
                    "goals":   goals,
                    "assists": assists,
                    "points":  goals + assists,
                    "toi_min": toi_min,
                })
            return logs
        except Exception:
            if attempt < RETRY_MAX:
                time.sleep(RETRY_DELAY)
    return []


def compute_stats(logs, pos="F"):
    """Modele V7 + ponderation exponentielle decroissante.
    Matchs recents ont plus de poids (decay=0.93, plancher=0.3).
    """
    df      = pd.DataFrame(logs)
    n       = len(df)
    avg_toi = df["toi_min"].sum() / n

    min_toi = MIN_TOI_DEFENSEMAN if pos == "D" else MIN_TOI_FORWARD
    if avg_toi < min_toi:
        return None
    if (df["points"] >= 1).sum() / n < MIN_SCORE_RATE:
        return None

    # Ponderation exponentielle : logs[0] = match le plus recent
    DECAY   = 0.93
    FLOOR   = 0.3
    weights = [max(DECAY ** i, FLOOR) for i in range(n)]
    total_w = sum(weights)
    avg_pts = sum(df["points"].iloc[i] * weights[i] for i in range(n)) / total_w

    expected_pts = avg_pts
    p_over       = round(1 - poisson.cdf(LINE, expected_pts), 4)
    return {
        "n_games": n, "avg_points": round(avg_pts, 3), "avg_toi": round(avg_toi, 1),
        "pts_per_60": round((avg_pts / avg_toi * 60) if avg_toi > 0 else 0, 4),
        "expected_pts": expected_pts, "p_over": p_over,
    }


def fetch_injury_report():
    """Retourne (set player_id blesses, liste pour affichage)."""
    url = "https://api-web.nhle.com/v1/injury-report"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        injured_ids  = set()
        injured_list = []
        for team_entry in r.json().get("InjuryReports", []):
            abbrev = team_entry.get("TeamAbbrev", "")
            for p in team_entry.get("InjuredPlayers", []):
                pid = p.get("PlayerId")
                if pid:
                    injured_ids.add(int(pid))
                injured_list.append({
                    "team":   abbrev,
                    "name":   p.get("Name", ""),
                    "status": p.get("InjuryStatus", ""),
                })
        return injured_ids, injured_list
    except Exception:
        return set(), []


def fetch_probable_goalies(game_id, home_abbrev, away_abbrev):
    """Gardiens probables via API NHL puis Daily Faceoff en fallback."""
    try:
        from bs4 import BeautifulSoup
        r = requests.get(
            "https://api-web.nhle.com/v1/gamecenter/{}/boxscore".format(game_id),
            timeout=10)
        r.raise_for_status()
        data    = r.json()
        home_id = data.get("homeTeam", {}).get("id")
        hg = ag = None
        for spot in data.get("rosterSpots", []):
            if spot.get("positionCode") != "G":
                continue
            name = "{} {}".format(
                spot.get("firstName", {}).get("default", ""),
                spot.get("lastName",  {}).get("default", ""),
            ).strip()
            if spot.get("teamId") == home_id:
                if not hg: hg = name
            else:
                if not ag: ag = name
        if hg or ag:
            return {"home": hg, "away": ag}
    except Exception:
        pass
    return {"home": None, "away": None}


def build_goalie_context(games):
    context = {}
    for g in games:
        gid     = g.get("game_id")
        if not gid: continue
        goalies = fetch_probable_goalies(gid, g["home"], g["away"])
        time.sleep(0.3)
        hg = goalies["home"] or "TBD"
        ag = goalies["away"] or "TBD"
        context[gid] = {
            "home_goalie":    hg,
            "away_goalie":    ag,
            "home_is_elite":  hg in ELITE_GOALIES,
            "away_is_elite":  ag in ELITE_GOALIES,
            "home_is_backup": (hg != "TBD") and (hg not in ELITE_GOALIES),
            "away_is_backup": (ag != "TBD") and (ag not in ELITE_GOALIES),
        }
    return context


def get_goalie_adj(game_id, is_home, goalie_context):
    ctx        = goalie_context.get(game_id, {})
    opp_elite  = ctx.get("away_is_elite",  False) if is_home else ctx.get("home_is_elite",  False)
    opp_backup = ctx.get("away_is_backup", False) if is_home else ctx.get("home_is_backup", False)
    if opp_backup: return  GOALIE_BACKUP_BONUS
    if opp_elite:  return -GOALIE_ELITE_MALUS
    return 0.0


def analyze_player(player, injured_ids=None, goalie_adj=0.0):
    if injured_ids and player["id"] in injured_ids:
        return None
    logs = fetch_game_logs(player["id"])
    if not logs or len(logs) < 3:
        return None
    pos   = player.get("pos", "F")
    stats = compute_stats(logs, pos=pos)
    if not stats:
        return None
    # Ajustement gardien
    expected_adj = round(stats["expected_pts"] * (1.0 + goalie_adj), 4)
    p_over_adj   = round(1 - poisson.cdf(LINE, expected_adj), 4)
    return {
        "id":            player["id"],
        "name":          player["name"],
        "team":          player["team"],
        "pos":           pos,
        "avg_points":    stats["avg_points"],
        "avg_toi":       stats["avg_toi"],
        "expected":      expected_adj,
        "p_over":        p_over_adj,
        "p_over_pct":    round(p_over_adj * 100, 1),
        "n_games":       stats["n_games"],
        "goalie_adj":    goalie_adj,
        "result":        None,
        "points_scored": None,
    }


# ============================================================
# VERIFICATION DES RESULTATS DE LA VEILLE
# ============================================================

def check_yesterday_results(history):
    """
    Pour chaque joueur du top 20 de la veille dont result=None,
    recupere ses game-logs et verifie s il a joue et marque.
    """
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    day_entry = None
    for day in history["days"]:
        if day["date"] == yesterday:
            day_entry = day
            break

    if not day_entry:
        print("Pas d entree hier dans l historique.")
        return history

    print("Verification des resultats du {}...".format(yesterday))
    updated = 0

    for p in day_entry["players"]:
        if p.get("result") is not None:
            continue

        player_id = p["id"]
        logs = fetch_game_logs(player_id)
        time.sleep(0.2)

        points_yesterday = None
        for log in logs:
            if log["date"] == yesterday:
                points_yesterday = log["points"]
                break

        if points_yesterday is None:
            p["result"]        = "no_game"
            p["points_scored"] = None
        else:
            p["result"]        = "win" if points_yesterday >= 1 else "loss"
            p["points_scored"] = points_yesterday
            updated += 1

    played = [p for p in day_entry["players"] if p.get("result") in ("win", "loss")]
    wins   = [p for p in played if p.get("result") == "win"]
    day_entry["success_rate"] = round(len(wins) / len(played) * 100, 1) if played else None
    day_entry["wins"]         = len(wins)
    day_entry["played"]       = len(played)

    print("Resultats d hier : {}/{} joueurs verifies".format(updated, len(day_entry["players"])))
    return history


# ============================================================
# THE ODDS API
# ============================================================

def fetch_odds():
    """Cotes Betclic en priorite, fallback meilleure cote EU."""
    try:
        betclic_map = {}
        eu_map      = {}

        r = requests.get(
            "https://api.the-odds-api.com/v4/sports/icehockey_nhl/odds/",
            params={"apiKey": ODDS_API_KEY, "regions": "eu",
                    "markets": "player_points", "oddsFormat": "decimal",
                    "bookmakers": "betclic"},
            timeout=15)
        if r.status_code == 200:
            for event in r.json():
                for bm in event.get("bookmakers", []):
                    for market in bm.get("markets", []):
                        if market.get("key") != "player_points": continue
                        for outcome in market.get("outcomes", []):
                            if outcome.get("point", 0) != 0.5: continue
                            name  = outcome.get("name", "").lower().strip()
                            price = outcome.get("price", 0)
                            if name not in betclic_map or price > betclic_map[name]:
                                betclic_map[name] = price

        r2 = requests.get(
            "https://api.the-odds-api.com/v4/sports/icehockey_nhl/odds/",
            params={"apiKey": ODDS_API_KEY, "regions": "eu",
                    "markets": "player_points", "oddsFormat": "decimal"},
            timeout=15)
        if r2.status_code == 200:
            for event in r2.json():
                for bm in event.get("bookmakers", []):
                    for market in bm.get("markets", []):
                        if market.get("key") != "player_points": continue
                        for outcome in market.get("outcomes", []):
                            if outcome.get("point", 0) != 0.5: continue
                            name  = outcome.get("name", "").lower().strip()
                            price = outcome.get("price", 0)
                            if name not in eu_map or price > eu_map[name]:
                                eu_map[name] = price

        final = {}
        for name in set(betclic_map) | set(eu_map):
            if name in betclic_map:
                final[name] = (betclic_map[name], "Betclic")
            else:
                final[name] = (eu_map[name], "EU")
        print("     {} cotes recuperees".format(len(final)))
        return final
    except Exception as e:
        print("     Odds API erreur : {}".format(e))
        return {}


def compute_value_picks(top_players, odds_map):
    value_picks = []
    for p in top_players:
        name_key = p["name"].lower().strip()
        if name_key in odds_map:
            cote, source = odds_map[name_key]
        else:
            cote, source = COTE_DEFAUT, "defaut"
        value_pct = round(((p["p_over"] * cote) - 1) * 100, 1)
        p["cote"]        = cote
        p["cote_source"] = source
        p["value_pct"]   = value_pct
        if value_pct >= VALUE_MIN_PCT:
            value_picks.append(p)
    return sorted(value_picks, key=lambda x: x["value_pct"], reverse=True)


# ============================================================
# CALCUL DU TAUX GLOBAL
# ============================================================

def compute_global_stats(history):
    total_played = 0
    total_wins   = 0
    for day in history["days"]:
        for p in day["players"]:
            if p.get("result") in ("win", "loss"):
                total_played += 1
                if p.get("result") == "win":
                    total_wins += 1
    rate = round(total_wins / total_played * 100, 1) if total_played > 0 else None
    return {"total_played": total_played, "total_wins": total_wins, "rate": rate}


# ============================================================
# CALCUL DES COMBINES
# ============================================================

def compute_combos(top_players, games, sizes=(2, 3, 4), top_n=5):
    """
    Genere les meilleurs combines de taille 2, 3 et 4 joueurs.
    Contrainte stricte : pas deux joueurs du meme match,
    meme s ils jouent dans des equipes adverses.
    """
    # Construit un dict team -> match_id a partir du vrai schedule du jour
    team_to_match = {}
    for idx, g in enumerate(games):
        team_to_match[g["home"]] = idx
        team_to_match[g["away"]] = idx

    result = {}
    for size in sizes:
        combos = []
        for group in combinations(top_players, size):
            # Recupere le match_id de chaque joueur
            match_ids = []
            valid = True
            for p in group:
                mid = team_to_match.get(p["team"])
                if mid is None:
                    # Equipe introuvable dans le schedule (edge case) -> on accepte
                    mid = "unknown_{}".format(p["team"])
                if mid in match_ids:
                    valid = False
                    break
                match_ids.append(mid)

            if not valid:
                continue   # deux joueurs du meme match -> interdit

            # Calcule la probabilite du combine (independance supposee)
            prob = 1.0
            for p in group:
                prob *= p["p_over"]
            prob_pct = round(prob * 100, 1)
            cote     = round(1 / prob, 2) if prob > 0 else None

            combos.append({
                "players":  list(group),
                "prob":     prob,
                "prob_pct": prob_pct,
                "cote":     cote,
            })

        combos.sort(key=lambda x: x["prob"], reverse=True)
        result[size] = combos[:top_n]

    return result


# ============================================================
# GENERATION HTML
# ============================================================

def build_combos_html(combos):
    """Genere le HTML de l onglet combines."""
    size_labels = {2: "Double", 3: "Triple", 4: "Quadruple"}
    size_colors = {2: "var(--cyan)", 3: "var(--gold)", 4: "#c77dff"}

    sub_tabs   = ""
    sub_panels = ""

    for i, size in enumerate((2, 3, 4)):
        label  = size_labels[size]
        color  = size_colors[size]
        active = "active" if i == 0 else ""
        display = "block" if i == 0 else "none"

        sub_tabs += '<button class="combo-tab {active}" data-panel="combo-{size}" onclick="showCombo(this)" style="border-bottom-color:{color}">{label}</button>'.format(
            active=active, size=size, color=color if active else "transparent", label=label)

        list_items = ""
        combos_list = combos.get(size, [])
        if not combos_list:
            list_items = '<div style="color:var(--muted);font-size:0.72rem;padding:1rem 0">Pas assez de joueurs de matchs differents pour generer des combines.</div>'
        else:
            for rank, c in enumerate(combos_list, 1):
                # Barre de probabilite
                prob   = c["prob_pct"]
                bar_color = "#1a5cff" if prob >= 50 else ("#e63329" if prob >= 30 else "#aaa")
                cote_str  = "{:.2f}".format(c["cote"]) if c["cote"] else "--"

                # Joueurs du combine
                players_html = ""
                for p in c["players"]:
                    p_prob = p["p_over_pct"]
                    players_html += '<div class="combo-player"><span class="combo-pname">{name}</span><span class="combo-pteam">{team}</span><span class="combo-pprob" style="color:{color}">{prob}%</span></div>'.format(
                        name=p["name"], team=p["team"],
                        color="var(--blue)" if p_prob >= 70 else ("var(--red)" if p_prob >= 60 else "#aaa"),
                        prob=p_prob
                    )

                list_items += """
                <div class="combo-card">
                  <div class="combo-header">
                    <span class="combo-rank">#{rank}</span>
                    <div class="combo-probs">
                      <span class="combo-prob-val" style="color:{bar_color}">{prob}%</span>
                      <span class="combo-cote">cote theo. <strong>{cote}</strong></span>
                    </div>
                  </div>
                  <div class="combo-prob-bar">
                    <div class="combo-prob-fill" style="width:0%;background:{bar_color}" data-target="{prob}"></div>
                  </div>
                  <div class="combo-players">{players}</div>
                </div>""".format(
                    rank=rank, bar_color=bar_color, prob=prob,
                    cote=cote_str, players=players_html
                )

        sub_panels += '<div class="combo-panel" id="combo-{size}" style="display:{display}">{items}</div>'.format(
            size=size, display=display, items=list_items)

    return """
    <div class="section-title" style="margin-top:1.5rem">Combines du jour</div>
    <p class="combo-intro">Combines optimaux issus du Top 20 · Pas deux joueurs du meme match · Probabilites independantes</p>
    <div class="combo-tabs">{sub_tabs}</div>
    <div class="combo-panels">{sub_panels}</div>""".format(
        sub_tabs=sub_tabs, sub_panels=sub_panels)




# ============================================================
# THE ODDS API
# ============================================================

def fetch_odds():
    """Cotes Betclic prioritaires, fallback meilleure cote EU. Retourne {name_lower: (cote, source)}."""
    try:
        betclic_map = {}
        eu_map      = {}

        # Appel Betclic
        r = requests.get(
            "https://api.the-odds-api.com/v4/sports/icehockey_nhl/odds/",
            params={"apiKey": ODDS_API_KEY, "regions": "eu",
                    "markets": "player_points", "oddsFormat": "decimal",
                    "bookmakers": "betclic"},
            timeout=15)
        if r.status_code == 200:
            for event in r.json():
                for bm in event.get("bookmakers", []):
                    for market in bm.get("markets", []):
                        if market.get("key") != "player_points": continue
                        for outcome in market.get("outcomes", []):
                            if outcome.get("point", 0) != 0.5: continue
                            name  = outcome.get("name", "").lower().strip()
                            price = outcome.get("price", 0)
                            if name not in betclic_map or price > betclic_map[name]:
                                betclic_map[name] = price

        # Appel fallback EU
        r2 = requests.get(
            "https://api.the-odds-api.com/v4/sports/icehockey_nhl/odds/",
            params={"apiKey": ODDS_API_KEY, "regions": "eu",
                    "markets": "player_points", "oddsFormat": "decimal"},
            timeout=15)
        if r2.status_code == 200:
            for event in r2.json():
                for bm in event.get("bookmakers", []):
                    for market in bm.get("markets", []):
                        if market.get("key") != "player_points": continue
                        for outcome in market.get("outcomes", []):
                            if outcome.get("point", 0) != 0.5: continue
                            name  = outcome.get("name", "").lower().strip()
                            price = outcome.get("price", 0)
                            if name not in eu_map or price > eu_map[name]:
                                eu_map[name] = price

        # Merge Betclic prioritaire
        final = {}
        for name in set(betclic_map) | set(eu_map):
            if name in betclic_map:
                final[name] = (betclic_map[name], "Betclic")
            else:
                final[name] = (eu_map[name], "EU")
        print("     {} cotes recuperees".format(len(final)))
        return final
    except Exception as e:
        print("     [Odds API] erreur : {}".format(e))
        return {}


def build_html(games, top_players, history, generated_at, odds_map=None, value_picks=None):
    today     = datetime.now().strftime("%d/%m/%Y")
    today_iso = datetime.now().strftime("%Y-%m-%d")
    matchups  = " · ".join(["{} vs {}".format(g["away"], g["home"]) for g in games])
    if odds_map  is None: odds_map    = {}
    if value_picks is None: value_picks = []

    # Calcul des combines
    combos      = compute_combos(top_players, games) if top_players else {2: [], 3: [], 4: []}
    combos_html = build_combos_html(combos)
    global_stats = compute_global_stats(history)

    # Taux global banner
    if global_stats["rate"] is not None:
        rate_color = "var(--blue)" if global_stats["rate"] >= 60 else ("var(--red)" if global_stats["rate"] >= 50 else "#aaa")
        global_banner = """
    <div class="global-rate">
      <div class="global-rate-label">TAUX DE REUSSITE GLOBAL</div>
      <div class="global-rate-value" style="color:{color}">{rate}%</div>
      <div class="global-rate-sub">{wins} picks gagnants sur {total} joues depuis le debut</div>
    </div>""".format(
            color=rate_color, rate=global_stats["rate"],
            wins=global_stats["total_wins"], total=global_stats["total_played"]
        )
    else:
        global_banner = """
    <div class="global-rate">
      <div class="global-rate-label">TAUX DE REUSSITE GLOBAL</div>
      <div class="global-rate-value" style="color:var(--muted)">--</div>
      <div class="global-rate-sub">Pas encore de resultats verifies</div>
    </div>"""

    # Lignes tableau top du jour
    rows_today = ""
    for i, p in enumerate(top_players):
        prob       = p["p_over_pct"]
        prob_class = "prob-high" if prob >= 70 else ("prob-mid" if prob >= 60 else "prob-low")
        pct_color  = "var(--blue)" if prob >= 70 else ("var(--red)" if prob >= 60 else "#aaa")
        signal     = '<span class="over-tag over-strong">UP FORT</span>' if prob >= 70 else \
                     ('<span class="over-tag over-mid">UP MID</span>' if prob >= 62 else "--")
        name_key   = p["name"].lower().strip()
        cote_val, cote_src = odds_map.get(name_key, (COTE_DEFAUT, "defaut"))
        value_pct  = round(((p["p_over"] * cote_val) - 1) * 100, 1)
        cote_color = "var(--red)" if value_pct >= VALUE_MIN_PCT else "var(--muted)"
        cote_str   = '<span style="color:{c};font-weight:600">{v}</span>'.format(c=cote_color, v=cote_val)
        rows_today += """
        <tr style="animation:fadeIn 0.4s ease {delay}s both">
          <td class="rank">{rank}</td>
          <td class="name">{name}</td>
          <td class="team">{team}</td>
          <td>{avg:.2f}</td>
          <td>{toi:.1f}</td>
          <td>{exp:.2f}</td>
          <td>
            <div class="prob-bar-wrap">
              <div class="prob-bar">
                <div class="prob-fill {pclass}" style="width:0%" data-target="{prob}"></div>
              </div>
              <span class="prob-pct" style="color:{pcolor}">{prob}%</span>
            </div>
          </td>
          <td>{cote}</td>
          <td>{signal}</td>
        </tr>""".format(
            delay=i*0.05, rank=i+1, name=p["name"], team=p["team"],
            avg=p["avg_points"], toi=p["avg_toi"], exp=p["expected"],
            pclass=prob_class, prob=prob, pcolor=pct_color,
            cote=cote_str, signal=signal
        )

    # Onglets historique
    history_tabs   = ""
    history_panels = ""
    days_sorted = sorted(history["days"], key=lambda d: d["date"], reverse=True)

    for day in days_sorted:
        if day["date"] == today_iso:
            continue
        date_fmt = datetime.strptime(day["date"], "%Y-%m-%d").strftime("%d/%m")
        rate     = day.get("success_rate")
        wins     = day.get("wins", 0)
        played   = day.get("played", 0)

        if rate is not None:
            tab_color = "var(--blue)" if rate >= 60 else ("var(--red)" if rate >= 50 else "#aaa")
            tab_badge = '<span class="tab-rate" style="color:{}">{:.0f}%</span>'.format(tab_color, rate)
        else:
            tab_badge = '<span class="tab-rate" style="color:var(--muted)">--</span>'

        history_tabs += '<button class="hist-tab" data-panel="day-{date}" onclick="showPanel(this)">{fmt} {badge}</button>'.format(
            date=day["date"], fmt=date_fmt, badge=tab_badge)

        panel_rows = ""
        for p in day["players"]:
            result = p.get("result")
            pts    = p.get("points_scored")
            if result == "win":
                res_icon = '<span class="res-win">V</span>'
                pts_str  = "+{} pt{}".format(pts, "s" if pts and pts > 1 else "")
            elif result == "loss":
                res_icon = '<span class="res-loss">X</span>'
                pts_str  = "0 pt"
            elif result == "no_game":
                res_icon = '<span class="res-nogame">-</span>'
                pts_str  = "repos"
            else:
                res_icon = '<span class="res-pending">?</span>'
                pts_str  = "en attente"

            panel_rows += """
            <tr>
              <td class="res-cell">{icon}</td>
              <td class="name">{name}</td>
              <td class="team">{team}</td>
              <td>{avg:.2f}</td>
              <td>{prob}%</td>
              <td>{pts}</td>
            </tr>""".format(
                icon=res_icon, name=p["name"], team=p["team"],
                avg=p["avg_points"], prob=p["p_over_pct"], pts=pts_str
            )

        rate_info = "{}/{} correct ({:.0f}%)".format(wins, played, rate) if rate is not None else "Resultats en attente"
        date_long = datetime.strptime(day["date"], "%Y-%m-%d").strftime("%d/%m/%Y")

        history_panels += """
        <div class="hist-panel" id="day-{date}" style="display:none">
          <div class="hist-panel-header">
            <span>Top 20 du {date_long}</span>
            <span class="panel-rate">{rate_info}</span>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr><th></th><th>Joueur</th><th>Equipe</th><th>Avg PTS</th><th>P(Over)</th><th>Resultat</th></tr>
              </thead>
              <tbody>{rows}</tbody>
            </table>
          </div>
        </div>""".format(
            date=day["date"], date_long=date_long,
            rate_info=rate_info, rows=panel_rows
        )

    if not history_tabs:
        history_tabs   = '<span style="color:var(--muted);font-size:0.72rem">Aucun historique disponible pour l instant. Les resultats apparaitront des le lendemain.</span>'
        history_panels = ""

    no_games_banner = ""
    if not games:
        no_games_banner = '<div class="no-games">Aucun match NHL programme aujourd hui.</div>'

    # Value bets HTML
    if value_picks:
        vrows = ""
        for i, p in enumerate(value_picks):
            vc        = "var(--red)" if p["value_pct"] >= 10 else "var(--blue)"
            gadj      = p.get("goalie_adj", 0)
            gadj_str  = ('+{:.0f}%'.format(gadj*100) if gadj > 0 else
                         ('{:.0f}%'.format(gadj*100) if gadj < 0 else '—'))
            gadj_col  = "#00b4d8" if gadj > 0 else ("#c1121f" if gadj < 0 else "var(--muted)")
            src_color = "#00b4d8" if p.get("cote_source","") == "Betclic" else "var(--muted)"
            vrows += (
                '<tr style="animation:fadeIn 0.4s ease {delay}s both">'
                '<td class="rank">{rank}</td>'
                '<td class="name">{name}</td>'
                '<td class="team">{team}</td>'
                '<td>{prob}%</td>'
                '<td style="color:var(--gold);font-weight:600">{cote}</td>'
                '<td style="color:{vc};font-weight:700">{val:+.1f}%</td>'
                '<td style="color:{gc}">{gadj}</td>'
                '<td style="color:{sc};font-size:0.62rem">{src}</td>'
                '</tr>'
            ).format(
                delay=i*0.06, rank=i+1, name=p["name"], team=p["team"],
                prob=p["p_over_pct"], cote=p.get("cote", COTE_DEFAUT),
                vc=vc, val=p["value_pct"], gc=gadj_col, gadj=gadj_str,
                sc=src_color, src=p.get("cote_source","defaut")
            )
        value_bets_html = (
            '<div class="section-title" style="margin-top:1.5rem">Value Bets &middot; {today}</div>'
            '<p style="font-size:0.68rem;color:var(--muted);margin-bottom:1.25rem;line-height:1.6">'
            'Value = (prob modele &times; cote bookmaker) &minus; 1 &gt; {thresh}%.'
            ' Betclic en priorite, fallback EU.'
            '</p>'
            '<div class="table-wrap"><table>'
            '<thead><tr>'
            '<th>#</th><th>Joueur</th><th>Equipe</th>'
            '<th>P(Over 0.5)</th><th>Cote</th><th>Value</th><th>Adj. Gardien</th><th>Source</th>'
            '</tr></thead>'
            '<tbody>{vrows}</tbody>'
            '</table></div>'
        ).format(today=today, thresh=VALUE_MIN_PCT, vrows=vrows)
    else:
        value_bets_html = (
            '<div class="section-title" style="margin-top:1.5rem">Value Bets &middot; {today}</div>'
            '<div class="no-games" style="margin-top:1rem">'
            'Aucun value bet &ge; {thresh}% detecte aujourd hui.'
            '</div>'
        ).format(today=today, thresh=VALUE_MIN_PCT)

    html = """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>NHL STATS BOT</title>
  <link href="https://fonts.googleapis.com/css2?family=Bangers&family=Barlow+Condensed:wght@400;600;700;900&family=Share+Tech+Mono&display=swap" rel="stylesheet"/>
  <style>
    :root{{
      --ink:#0a0a0a;
      --paper:#f5f0e8;
      --red:#e63329;
      --yellow:#ffd500;
      --blue:#1a5cff;
      --cyan:#00d4ff;
      --magenta:#ff2d9b;
      --white:#ffffff;
      --muted:#555;
      --border:3px solid var(--ink);
    }}
    *{{margin:0;padding:0;box-sizing:border-box;}}

    /* HALFTONE BACKGROUND */
    body{{
      background-color:var(--ink);
      color:var(--ink);
      font-family:'Barlow Condensed',sans-serif;
      overflow-x:hidden;
      position:relative;
    }}
    body::before{{
      content:'';
      position:fixed;inset:0;pointer-events:none;z-index:0;
      background-image:radial-gradient(circle,rgba(255,213,0,0.08) 1px,transparent 1px);
      background-size:18px 18px;
    }}

    /* SCAN LINE OVERLAY */
    body::after{{
      content:'';
      position:fixed;inset:0;pointer-events:none;z-index:1;
      background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,0.04) 2px,rgba(0,0,0,0.04) 4px);
    }}

    /* ===== HEADER ===== */
    header{{
      position:relative;z-index:10;
      background:var(--yellow);
      border-bottom:5px solid var(--ink);
      padding:0;
      overflow:hidden;
    }}
    .header-top{{
      background:var(--red);
      border-bottom:4px solid var(--ink);
      padding:0.4rem 2rem;
      display:flex;justify-content:space-between;align-items:center;
    }}
    .header-top-label{{
      font-family:'Bangers',cursive;
      font-size:0.9rem;
      letter-spacing:0.15em;
      color:var(--yellow);
    }}
    .header-main{{
      padding:1.5rem 2rem 1rem;
      text-align:center;
      position:relative;
    }}
    /* diagonal stripe accent */
    .header-main::before{{
      content:'';
      position:absolute;top:0;left:0;right:0;bottom:0;
      background:repeating-linear-gradient(
        -45deg,
        transparent,transparent 10px,
        rgba(0,0,0,0.04) 10px,rgba(0,0,0,0.04) 20px
      );
      pointer-events:none;
    }}
    h1{{
      font-family:'Bangers',cursive;
      font-size:clamp(3.5rem,10vw,7rem);
      letter-spacing:0.06em;
      color:var(--ink);
      line-height:0.9;
      text-shadow:6px 6px 0 var(--red),-2px -2px 0 rgba(0,0,255,0.3);
      position:relative;
    }}
    h1 .accent{{color:var(--red);text-shadow:4px 4px 0 var(--ink);}}
    h1 .accent2{{color:var(--blue);text-shadow:4px 4px 0 var(--ink);}}
    .tagline{{
      font-family:'Bangers',cursive;
      font-size:1.1rem;
      letter-spacing:0.2em;
      color:var(--ink);
      margin-top:0.3rem;
      opacity:0.7;
    }}
    .meta-pill{{
      display:inline-block;
      background:var(--ink);
      color:var(--yellow);
      font-family:'Share Tech Mono',monospace;
      font-size:0.65rem;
      letter-spacing:0.12em;
      padding:0.25rem 0.75rem;
      margin-top:0.5rem;
      clip-path:polygon(8px 0%,100% 0%,calc(100% - 8px) 100%,0% 100%);
    }}
    .meta-pill strong{{color:var(--cyan);}}

    /* GLOBAL RATE — comic panel style */
    .global-rate{{
      margin:1rem auto 0;
      max-width:380px;
      background:var(--white);
      border:4px solid var(--ink);
      box-shadow:6px 6px 0 var(--ink);
      padding:1rem 1.5rem;
      text-align:center;
      position:relative;
    }}
    .global-rate::before{{
      content:'STATS';
      position:absolute;top:-14px;left:16px;
      background:var(--blue);
      color:var(--white);
      font-family:'Bangers',cursive;
      font-size:0.75rem;
      letter-spacing:0.15em;
      padding:0 0.5rem;
      border:3px solid var(--ink);
    }}
    .global-rate-label{{font-size:0.55rem;letter-spacing:0.25em;color:var(--muted);text-transform:uppercase;margin-bottom:0.2rem;font-weight:700;}}
    .global-rate-value{{font-family:'Bangers',cursive;font-size:3.5rem;letter-spacing:0.05em;line-height:1;color:var(--ink);}}
    .global-rate-sub{{font-size:0.65rem;color:var(--muted);margin-top:0.2rem;font-weight:600;}}

    /* ===== TICKER ===== */
    .ticker{{
      overflow:hidden;
      background:var(--ink);
      border-top:3px solid var(--yellow);
      border-bottom:3px solid var(--yellow);
      padding:0.45rem 0;
      position:relative;z-index:10;
    }}
    .ticker-inner{{display:flex;gap:0;animation:ticker 32s linear infinite;white-space:nowrap;}}
    @keyframes ticker{{from{{transform:translateX(0);}}to{{transform:translateX(-50%);}}}}
    .ticker-item{{
      font-family:'Bangers',cursive;
      font-size:0.85rem;
      letter-spacing:0.12em;
      color:var(--yellow);
      padding:0 2rem;
      border-right:2px solid rgba(255,213,0,0.3);
    }}
    .ticker-item strong{{color:var(--cyan);}}
    .ticker-sep{{color:var(--red);padding:0 0.5rem;}}

    /* ===== NAVIGATION TABS ===== */
    .tabs{{
      display:flex;
      background:var(--ink);
      position:sticky;top:0;z-index:50;
      border-bottom:4px solid var(--yellow);
      overflow-x:auto;
    }}
    .tab-btn{{
      background:transparent;
      border:none;
      border-right:2px solid rgba(255,213,0,0.2);
      padding:0.9rem 1.8rem;
      font-family:'Bangers',cursive;
      font-size:1rem;
      letter-spacing:0.15em;
      color:rgba(255,213,0,0.5);
      cursor:pointer;
      transition:all 0.15s;
      white-space:nowrap;
      position:relative;
    }}
    .tab-btn:hover{{color:var(--yellow);background:rgba(255,213,0,0.08);}}
    .tab-btn.active{{
      color:var(--ink);
      background:var(--yellow);
    }}
    .tab-btn.active::after{{
      content:'';
      position:absolute;bottom:-8px;left:50%;transform:translateX(-50%);
      border:6px solid transparent;
      border-top-color:var(--yellow);
    }}
    .tab-content{{display:none;}}
    .tab-content.active{{display:block;}}

    /* ===== MAIN ===== */
    main{{
      position:relative;z-index:10;
      max-width:1140px;
      margin:0 auto;
      padding:2rem 1.5rem 4rem;
    }}

    /* Section title — speech bubble style */
    .section-title{{
      font-family:'Bangers',cursive;
      font-size:1.6rem;
      letter-spacing:0.1em;
      color:var(--yellow);
      margin-bottom:1.2rem;
      margin-top:1.5rem;
      display:inline-flex;
      align-items:center;
      gap:0.75rem;
      background:var(--ink);
      padding:0.3rem 1.2rem 0.3rem 0.8rem;
      clip-path:polygon(0 0,calc(100% - 12px) 0,100% 50%,calc(100% - 12px) 100%,0 100%);
    }}

    /* Matchups bar */
    .matchups-bar{{
      background:var(--white);
      border:3px solid var(--ink);
      box-shadow:4px 4px 0 var(--ink);
      padding:0.6rem 1.1rem;
      margin-bottom:1.5rem;
      font-family:'Share Tech Mono',monospace;
      font-size:0.72rem;
      letter-spacing:0.06em;
    }}
    .matchups-bar span{{color:var(--red);font-weight:700;margin-right:0.5rem;}}

    .no-games{{
      background:var(--red);
      border:3px solid var(--ink);
      box-shadow:4px 4px 0 var(--ink);
      padding:1rem;
      margin-bottom:1.5rem;
      text-align:center;
      color:var(--white);
      font-family:'Bangers',cursive;
      font-size:1.1rem;
      letter-spacing:0.1em;
    }}

    /* ===== TABLE — comic panel ===== */
    .table-wrap{{
      overflow-x:auto;
      border:4px solid var(--ink);
      box-shadow:7px 7px 0 var(--yellow);
      margin-bottom:2.5rem;
      background:var(--white);
    }}
    table{{width:100%;border-collapse:collapse;font-size:0.78rem;}}
    thead tr{{background:var(--ink);}}
    th{{
      padding:0.7rem 0.9rem;
      text-align:left;
      font-family:'Bangers',cursive;
      letter-spacing:0.12em;
      color:var(--yellow);
      font-size:0.8rem;
      white-space:nowrap;
      border-right:1px solid rgba(255,213,0,0.2);
    }}
    tbody tr{{border-bottom:2px solid var(--ink);transition:background 0.1s;}}
    tbody tr:nth-child(even){{background:#f0ebe0;}}
    tbody tr:hover{{background:var(--yellow);}}
    td{{padding:0.6rem 0.9rem;color:var(--ink);white-space:nowrap;font-weight:600;border-right:1px solid rgba(0,0,0,0.08);}}
    td.rank{{
      font-family:'Bangers',cursive;
      font-size:1.4rem;
      color:var(--red);
      width:40px;
      text-align:center;
    }}
    td.name{{color:var(--ink);font-weight:900;font-size:0.85rem;text-transform:uppercase;letter-spacing:0.03em;}}
    td.team{{
      color:var(--blue);
      font-family:'Bangers',cursive;
      font-size:0.9rem;
      letter-spacing:0.1em;
    }}

    /* PROB BAR */
    .prob-bar-wrap{{display:flex;align-items:center;gap:0.5rem;}}
    .prob-bar{{
      flex:1;height:8px;
      background:rgba(0,0,0,0.12);
      border:2px solid var(--ink);
      overflow:hidden;
      min-width:55px;
    }}
    .prob-fill{{height:100%;transition:width 0.8s cubic-bezier(0.34,1.56,0.64,1);}}
    .prob-high{{background:var(--blue);}}
    .prob-mid{{background:var(--red);}}
    .prob-low{{background:#aaa;}}
    .prob-pct{{
      font-family:'Bangers',cursive;
      font-size:0.9rem;
      min-width:38px;
      text-align:right;
    }}

    /* SIGNAL TAGS */
    .over-tag{{
      display:inline-block;
      padding:0.1rem 0.45rem;
      font-family:'Bangers',cursive;
      font-size:0.75rem;
      letter-spacing:0.08em;
      border:2px solid var(--ink);
    }}
    .over-strong{{background:var(--blue);color:var(--white);}}
    .over-mid{{background:var(--red);color:var(--white);}}

    /* ===== HISTORY TABS ===== */
    .hist-tabs{{display:flex;flex-wrap:wrap;gap:0.4rem;margin-bottom:1.25rem;}}
    .hist-tab{{
      background:var(--ink);
      border:2px solid rgba(255,213,0,0.3);
      padding:0.35rem 0.8rem;
      font-family:'Bangers',cursive;
      font-size:0.85rem;
      letter-spacing:0.1em;
      color:rgba(255,213,0,0.6);
      cursor:pointer;
      transition:all 0.15s;
      display:inline-flex;align-items:center;gap:0.4rem;
    }}
    .hist-tab:hover,.hist-tab.active{{
      background:var(--yellow);
      color:var(--ink);
      border-color:var(--yellow);
    }}
    .tab-rate{{font-size:0.75rem;font-weight:700;}}
    .hist-panel-header{{
      display:flex;justify-content:space-between;align-items:center;
      margin-bottom:0.75rem;
      font-family:'Bangers',cursive;
      font-size:1rem;
      letter-spacing:0.08em;
      color:var(--yellow);
      padding:0.4rem 0.75rem;
      background:var(--ink);
      border:3px solid var(--yellow);
    }}
    .panel-rate{{color:var(--cyan);}}
    .res-cell{{width:30px;text-align:center;}}
    .res-win{{
      display:inline-block;
      background:var(--blue);color:var(--white);
      font-family:'Bangers',cursive;font-size:0.9rem;
      padding:0 5px;border:2px solid var(--ink);
    }}
    .res-loss{{
      display:inline-block;
      background:var(--red);color:var(--white);
      font-family:'Bangers',cursive;font-size:0.9rem;
      padding:0 5px;border:2px solid var(--ink);
    }}
    .res-nogame{{color:#aaa;font-family:'Bangers',cursive;}}
    .res-pending{{color:var(--yellow);font-family:'Bangers',cursive;}}

    /* ===== COMBO CARDS ===== */
    .combo-intro{{font-size:0.75rem;color:var(--yellow);margin-bottom:1.25rem;line-height:1.6;font-weight:600;letter-spacing:0.05em;}}
    .combo-tabs{{display:flex;gap:0;border-bottom:4px solid var(--yellow);margin-bottom:1.5rem;}}
    .combo-tab{{
      background:transparent;border:none;
      border-bottom:3px solid transparent;
      padding:0.6rem 1.4rem;
      font-family:'Bangers',cursive;
      font-size:1rem;letter-spacing:0.12em;
      color:rgba(255,213,0,0.5);cursor:pointer;transition:all 0.15s;
    }}
    .combo-tab:hover{{color:var(--yellow);}}
    .combo-tab.active{{color:var(--ink);background:var(--yellow);}}
    .combo-card{{
      background:var(--white);
      border:4px solid var(--ink);
      box-shadow:5px 5px 0 var(--ink);
      padding:1.1rem 1.25rem;
      margin-bottom:1rem;
      transition:transform 0.15s,box-shadow 0.15s;
      position:relative;
    }}
    .combo-card:hover{{transform:translate(-2px,-2px);box-shadow:7px 7px 0 var(--ink);}}
    .combo-header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:0.5rem;}}
    .combo-rank{{
      font-family:'Bangers',cursive;font-size:2rem;
      color:var(--red);line-height:1;
    }}
    .combo-probs{{display:flex;align-items:baseline;gap:1rem;}}
    .combo-prob-val{{font-family:'Bangers',cursive;font-size:2.2rem;letter-spacing:0.03em;line-height:1;}}
    .combo-cote{{font-size:0.7rem;color:var(--muted);font-weight:700;}}
    .combo-cote strong{{color:var(--red);}}
    .combo-prob-bar{{
      height:6px;
      background:rgba(0,0,0,0.1);
      border:2px solid var(--ink);
      overflow:hidden;margin-bottom:0.85rem;
    }}
    .combo-prob-fill{{height:100%;transition:width 0.9s cubic-bezier(0.34,1.56,0.64,1);}}
    .combo-players{{display:flex;flex-wrap:wrap;gap:0.5rem;}}
    .combo-player{{
      background:var(--ink);
      border:2px solid var(--ink);
      padding:0.3rem 0.65rem;
      display:flex;align-items:center;gap:0.5rem;
      clip-path:polygon(6px 0%,100% 0%,calc(100% - 6px) 100%,0% 100%);
    }}
    .combo-pname{{font-size:0.75rem;color:var(--yellow);font-weight:900;letter-spacing:0.04em;text-transform:uppercase;}}
    .combo-pteam{{font-size:0.62rem;color:rgba(255,213,0,0.5);letter-spacing:0.08em;}}
    .combo-pprob{{font-family:'Bangers',cursive;font-size:0.85rem;}}

    /* DIVIDER */
    .glow-line{{
      height:4px;
      background:repeating-linear-gradient(90deg,var(--yellow) 0,var(--yellow) 12px,var(--ink) 12px,var(--ink) 16px);
      margin:2.5rem 0;
      opacity:0.6;
    }}

    /* ===== FOOTER ===== */
    footer{{
      position:relative;z-index:10;
      text-align:center;
      padding:1.5rem;
      border-top:4px solid var(--yellow);
      background:var(--ink);
      font-family:'Bangers',cursive;
      font-size:0.85rem;
      letter-spacing:0.12em;
      color:rgba(255,213,0,0.5);
    }}
    footer a{{color:var(--cyan);text-decoration:none;margin:0 0.5rem;}}
    footer a:hover{{color:var(--yellow);}}

    /* ===== ANIMATIONS ===== */
    @keyframes fadeIn{{
      from{{opacity:0;transform:translateY(10px) skewX(-2deg);}}
      to{{opacity:1;transform:translateY(0) skewX(0deg);}}
    }}
    @keyframes glitch{{
      0%,100%{{text-shadow:6px 6px 0 var(--red),-2px -2px 0 rgba(0,0,255,0.3);}}
      25%{{text-shadow:-4px 4px 0 var(--cyan),4px -2px 0 var(--red);}}
      75%{{text-shadow:4px -4px 0 var(--magenta),-2px 2px 0 var(--blue);}}
    }}
    h1{{animation:glitch 6s ease-in-out infinite;}}
  </style>
</head>
<body>

<header>
  <div class="header-top">
    <span class="header-top-label">&#9733; NHL STATS BOT v8.0 &#9733;</span>
    <span class="header-top-label" style="font-size:0.75rem;opacity:0.7">POISSON MODEL · API OFFICIELLE</span>
  </div>
  <div class="header-main">
    <h1>NHL <span class="accent">STATS</span> <span class="accent2">BOT</span></h1>
    <p class="tagline">Analyse automatique · Probabilites Poisson · Top {top_n} joueurs du jour</p>
    <div class="meta-pill">Mis a jour le <strong>{generated_at}</strong> &nbsp;|&nbsp; Matchs : <strong>{nb_games}</strong></div>
    {global_banner}
  </div>
</header>

<div class="ticker">
  <div class="ticker-inner">
    <span class="ticker-item"><strong>&#9733; MODE AUTO</strong> — Analyse tous les matchs NHL du jour</span>
    <span class="ticker-item"><strong>&#9733; POISSON MODEL</strong> — P(Over 0.5 pts) par joueur</span>
    <span class="ticker-item"><strong>&#9733; 15 MATCHS</strong> — Fenetre glissante game-logs</span>
    <span class="ticker-item"><strong>&#9733; VALUE BETS</strong> — Detection automatique</span>
    <span class="ticker-item"><strong>&#9733; API NHL</strong> — Donnees officielles nhle.com</span>
    <span class="ticker-item"><strong>&#9733; MODE AUTO</strong> — Analyse tous les matchs NHL du jour</span>
    <span class="ticker-item"><strong>&#9733; POISSON MODEL</strong> — P(Over 0.5 pts) par joueur</span>
    <span class="ticker-item"><strong>&#9733; 15 MATCHS</strong> — Fenetre glissante game-logs</span>
    <span class="ticker-item"><strong>&#9733; VALUE BETS</strong> — Detection automatique</span>
    <span class="ticker-item"><strong>&#9733; API NHL</strong> — Donnees officielles nhle.com</span>
  </div>
</div>

<div class="tabs">
  <button class="tab-btn active" onclick="showTab(this,'tab-today')">Aujourd hui</button>
  <button class="tab-btn" onclick="showTab(this,'tab-combines')">Combines</button>
  <button class="tab-btn" onclick="showTab(this,'tab-history')">Historique</button>
  <button class="tab-btn" onclick="showTab(this,'tab-value')">Value Bets &#9733;</button>
</div>

<main>
  <!-- ONGLET AUJOURD HUI -->
  <div class="tab-content active" id="tab-today">
    <div class="section-title">Matchs du jour</div>
    {no_games_banner}
    <div class="matchups-bar"><span>&#9660; {today}</span>{matchups}</div>
    <div class="section-title">Top {top_n} · {today}</div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>#</th><th>Joueur</th><th>Equipe</th>
            <th>Avg PTS</th><th>TOI moy</th>
            <th>Expected</th><th>P(Over 0.5)</th><th>Cote</th><th>Signal</th>
          </tr>
        </thead>
        <tbody>{rows_today}</tbody>
      </table>
    </div>
  </div>

  <!-- ONGLET COMBINES -->
  <div class="tab-content" id="tab-combines">
    {combos_html}
  </div>

  <!-- ONGLET VALUE BETS -->
  <div class="tab-content" id="tab-value">
    {value_bets_html}
  </div>

  <!-- ONGLET HISTORIQUE -->
  <div class="tab-content" id="tab-history">
    <div class="section-title">Historique des picks</div>
    <div class="hist-tabs">{history_tabs}</div>
    <div id="hist-panels">{history_panels}</div>
  </div>

  <div class="glow-line"></div>
</main>

<footer>
  <div>NHL STATS BOT &middot; v8.0 &middot; Python + NHL API officielle &middot; Genere le {generated_at}</div>
  <div style="margin-top:0.4rem">
    <a href="https://github.com/Thiboulard/NHL-Stats" target="_blank">GitHub</a>
    &middot; <a href="https://api-web.nhle.com" target="_blank">NHL API</a>
    &middot; Modele : Poisson
  </div>
</footer>

<script>
  function showTab(btn, tabId) {{
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(tabId).classList.add('active');
  }}
  function showPanel(btn) {{
    document.querySelectorAll('.hist-tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.hist-panel').forEach(p => p.style.display = 'none');
    btn.classList.add('active');
    var panel = document.getElementById(btn.dataset.panel);
    if (panel) panel.style.display = 'block';
  }}
  var comboColors = {{'combo-2':'var(--cyan)','combo-3':'var(--gold)','combo-4':'#c77dff'}};
  function showCombo(btn) {{
    document.querySelectorAll('.combo-tab').forEach(b => {{ b.classList.remove('active'); b.style.borderBottomColor='transparent'; b.style.color='var(--muted)'; }});
    document.querySelectorAll('.combo-panel').forEach(p => p.style.display = 'none');
    btn.classList.add('active');
    var color = comboColors[btn.dataset.panel] || 'var(--cyan)';
    btn.style.borderBottomColor = color;
    btn.style.color = '#fff';
    var panel = document.getElementById(btn.dataset.panel);
    if (panel) {{ panel.style.display = 'block'; animateComboBars(); }}
  }}
  function animateComboBars() {{
    setTimeout(function() {{
      document.querySelectorAll('.combo-prob-fill').forEach(function(el) {{
        el.style.width = el.dataset.target + '%';
      }});
    }}, 100);
  }}
  setTimeout(function() {{
    document.querySelectorAll('.prob-fill').forEach(function(el) {{
      el.style.width = el.dataset.target + '%';
    }});
    animateComboBars();
  }}, 300);
</script>

</body>
</html>""".format(
        top_n=TOP_N, generated_at=generated_at, nb_games=len(games),
        global_banner=global_banner, today=today,
        matchups=matchups if games else "Aucun match aujourd hui",
        no_games_banner=no_games_banner,
        rows_today=rows_today,
        combos_html=combos_html,
        value_bets_html=value_bets_html,
        history_tabs=history_tabs,
        history_panels=history_panels,
    )
    return html


# ============================================================
# POINT D ENTREE
# ============================================================

def main():
    generated_at = datetime.now().strftime("%d/%m/%Y a %H:%M")
    today_iso    = datetime.now().strftime("%Y-%m-%d")

    print("\n" + "="*55)
    print("  NHL STATS BOT - Generation page + historique")
    print("="*55)

    if not GITHUB_TOKEN:
        print("ATTENTION : GH_TOKEN manquant")

    # 1. Charge l historique
    print("\n[1/6] Chargement de l historique...")
    history, history_sha = load_history()
    print("     {} jours dans l historique".format(len(history["days"])))

    # 2. Verifie les resultats de la veille
    print("\n[2/6] Verification des resultats de la veille...")
    history = check_yesterday_results(history)

    # 3. Matchs du jour
    print("\n[3/6] Recuperation des matchs du jour...")
    games = fetch_todays_games()

    top = []
    if games:
        # 4. Rosters + injury report + gardiens
        print("\n[4/6] Recuperation des rosters + morning report...")
        injured_ids, injured_list = fetch_injury_report()
        print("     {} joueur(s) blesse(s)".format(len(injured_list)))

        goalie_context = build_goalie_context(games)
        confirmed = sum(1 for c in goalie_context.values() if c.get("home_goalie") != "TBD")
        print("     {}/{} gardiens confirmes".format(confirmed, len(games)))

        # Index team -> game_id + is_home
        team_to_game = {}
        team_is_home = {}
        for g in games:
            gid = g.get("game_id")
            team_to_game[g["home"]] = gid; team_to_game[g["away"]] = gid
            team_is_home[g["home"]] = True; team_is_home[g["away"]] = False

        all_players, teams_done = [], set()
        for g in games:
            for abbrev in [g["home"], g["away"]]:
                if abbrev in teams_done:
                    continue
                teams_done.add(abbrev)
                print("     Roster {}...".format(abbrev))
                all_players.extend(fetch_roster_by_team_abbrev(abbrev))
                time.sleep(0.3)

        # 5. Analyse
        print("\n[5/6] Analyse des joueurs ({} joueurs)...".format(len(all_players)))
        results = []
        for i, player in enumerate(all_players):
            print("     [{}/{}] {}...".format(i+1, len(all_players), player["name"]), end="\r")
            gid     = team_to_game.get(player["team"])
            is_home = team_is_home.get(player["team"], True)
            g_adj   = get_goalie_adj(gid, is_home, goalie_context) if gid else 0.0
            result  = analyze_player(player, injured_ids=injured_ids, goalie_adj=g_adj)
            if result:
                results.append(result)
            time.sleep(0.15)
        print("\n     {} joueurs analyses".format(len(results)))

        results.sort(key=lambda x: x["p_over"], reverse=True)
        top             = results[:TOP_N]                                          # affichage : top 20 sans filtre
        top_for_history = [p for p in top if p["p_over_pct"] >= MIN_PROB_HISTORY] # historique : >= 65% seulement

        # Cotes + value picks
        print("     Recuperation des cotes...")
        odds_map = fetch_odds()
        value_picks = []
        for p in top:
            name_key = p["name"].lower().strip()
            cote_val, cote_src = odds_map.get(name_key, (COTE_DEFAUT, "defaut"))
            vp = round(((p["p_over"] * cote_val) - 1) * 100, 1)
            if vp >= VALUE_MIN_PCT:
                value_picks.append({**p, "cote": cote_val, "value_pct": vp, "cote_source": cote_src})
        value_picks.sort(key=lambda x: x["value_pct"], reverse=True)
        print("     {} pick(s) value".format(len(value_picks)))

        # Ajoute ou met a jour le jour courant dans l historique
        history["days"] = [d for d in history["days"] if d["date"] != today_iso]
        history["days"].append({
            "date":         today_iso,
            "matchups":     ["{} vs {}".format(g["away"], g["home"]) for g in games],
            "players":      top_for_history,   # seuls les >= 65% dans l historique
            "success_rate": None,
            "wins":         0,
            "played":       0,
        })
        # Garde les 30 derniers jours
        history["days"] = sorted(history["days"], key=lambda d: d["date"], reverse=True)[:30]
    else:
        print("\n     Aucun match aujourd hui.")
        odds_map    = {}
        value_picks = []

    # 6. Sauvegarde et generation
    print("\n[6/6] Sauvegarde et generation...")
    if GITHUB_TOKEN:
        save_history(history, history_sha)
        content, sha = github_get(HTML_FILE)
        ok = github_put(HTML_FILE, build_html(games, top, history, generated_at, odds_map=odds_map, value_picks=value_picks),
                        "auto: update NHL page - {}".format(today_iso), sha)
        print("index.html pousse !" if ok else "Erreur push index.html")
    else:
        with open("index.html", "w", encoding="utf-8") as f:
            f.write(build_html(games, top, history, generated_at, odds_map=odds_map, value_picks=value_picks))
        print("index.html ecrit localement")

    print("\nDone!")


if __name__ == "__main__":
    main()
