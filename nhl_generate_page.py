"""
NHL STATS BOT - Générateur de page GitHub Pages
================================================
Ce script :
1. Récupère les matchs NHL du jour
2. Analyse les joueurs (game-logs + Poisson)
3. Génère index.html avec les vraies données
4. Pousse le fichier sur GitHub via l'API Git

Usage : python nhl_generate_page.py
"""

import requests
import pandas as pd
from scipy.stats import poisson
import numpy as np
from datetime import datetime
import time
import sys
import os
import json
import base64

# ============================================================
# CONFIGURATION
# ============================================================

LINE         = 0.5
TOI_EXPECTED = 22.0
SEASON       = "20252026"
LAST_N_GAMES = 15
RETRY_MAX    = 3
RETRY_DELAY  = 3
TOP_N        = 20

# GitHub - à remplir avec tes infos
GITHUB_TOKEN = os.environ.get("GH_TOKEN", "")   # injecté par GitHub Actions
GITHUB_REPO  = "Thiboulard/NHL-Stats"            # ton dépôt
GITHUB_FILE  = "index.html"                      # fichier à mettre à jour
GITHUB_BRANCH = "main"

# Telegram (optionnel ici, garde tes valeurs)
TELEGRAM_TOKEN = "8789707531:AAEqD4DZ-dRTl6Hq-1Rgzp8JiHjS8JdYqB8"
CHAT_ID        = "6704055547"

# ============================================================
# FONCTIONS NHL (identiques à ton script original)
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
                    "home_id": g.get("homeTeam", {}).get("id"),
                    "away_id": g.get("awayTeam", {}).get("id"),
                    "time":    g.get("startTimeUTC", ""),
                })
        print(">>> {} match(s) trouvé(s) ({})".format(len(games), today))
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
                players.append({
                    "id":   p.get("id"),
                    "name": "{} {}".format(fn, ln).strip(),
                    "pos":  p.get("positionCode", "?"),
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


def compute_stats(logs, toi_expected):
    df = pd.DataFrame(logs)
    total_points  = df["points"].sum()
    total_goals   = df["goals"].sum()
    total_assists = df["assists"].sum()
    total_toi     = df["toi_min"].sum()
    n_games       = len(df)
    avg_points = round(total_points / n_games, 4) if n_games > 0 else 0
    avg_toi    = round(total_toi    / n_games, 4) if n_games > 0 else 0
    pts_per_60 = round((total_points / total_toi) * 60, 4) if total_toi > 0 else 0
    expected_pts = round(avg_points * (toi_expected / avg_toi), 4) if avg_toi > 0 else avg_points
    p_over  = round(1 - poisson.cdf(LINE, expected_pts), 4)
    p_under = round(1 - p_over, 4)
    return {
        "n_games": n_games, "avg_points": avg_points, "avg_toi": avg_toi,
        "pts_per_60": pts_per_60, "expected_pts": expected_pts,
        "p_over": p_over, "p_under": p_under,
    }


def analyze_player(player):
    logs = fetch_game_logs(player["id"])
    if not logs or len(logs) < 3:
        return None
    df = pd.DataFrame(logs)
    avg_toi_reel = round(df["toi_min"].mean(), 2)
    stats = compute_stats(logs, avg_toi_reel)
    return {
        "id":         player["id"],
        "name":       player["name"],
        "team":       player["team"],
        "pos":        player["pos"],
        "avg_points": stats["avg_points"],
        "avg_toi":    stats["avg_toi"],
        "pts_per_60": stats["pts_per_60"],
        "expected":   stats["expected_pts"],
        "p_over":     stats["p_over"],
        "p_over_pct": round(stats["p_over"] * 100, 1),
        "n_games":    stats["n_games"],
    }


# ============================================================
# GÉNÉRATION HTML
# ============================================================

def build_html(games, top_players, generated_at):
    today    = datetime.now().strftime("%d/%m/%Y")
    matchups = " · ".join(["{} vs {}".format(g["away"], g["home"]) for g in games])

    # Lignes du tableau
    rows = ""
    for i, p in enumerate(top_players):
        prob       = p["p_over_pct"]
        prob_class = "prob-high" if prob >= 70 else ("prob-mid" if prob >= 60 else "prob-low")
        pct_color  = "var(--cyan)" if prob >= 70 else ("var(--gold)" if prob >= 60 else "var(--muted)")
        signal     = '<span class="over-tag over-strong">⬆ FORT</span>' if prob >= 70 else \
                     ('<span class="over-tag over-mid">⬆ MID</span>' if prob >= 62 else "—")
        rows += """
        <tr style="animation: fadeIn 0.4s ease {delay}s both">
          <td class="rank">{rank}</td>
          <td class="name">{name}</td>
          <td class="team">{team}</td>
          <td>{pos}</td>
          <td>{avg:.2f}</td>
          <td>{toi:.1f}</td>
          <td>{p60:.2f}</td>
          <td>{exp:.2f}</td>
          <td>
            <div class="prob-bar-wrap">
              <div class="prob-bar">
                <div class="prob-fill {pclass}" style="width:0%" data-target="{prob}"></div>
              </div>
              <span class="prob-pct" style="color:{pcolor}">{prob}%</span>
            </div>
          </td>
          <td>{signal}</td>
        </tr>""".format(
            delay=i * 0.06, rank=i+1, name=p["name"], team=p["team"], pos=p["pos"],
            avg=p["avg_points"], toi=p["avg_toi"], p60=p["pts_per_60"],
            exp=p["expected"], pclass=prob_class, prob=prob,
            pcolor=pct_color, signal=signal
        )

    no_games_banner = ""
    if not games:
        no_games_banner = '<div class="no-games">Aucun match NHL programmé aujourd\'hui.</div>'

    html = """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>NHL STATS BOT</title>
  <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=JetBrains+Mono:wght@300;400;600&display=swap" rel="stylesheet" />
  <style>
    :root {{
      --ice: #e8f4f8; --dark: #0a0e14; --navy: #0d1b2a;
      --blue: #0077b6; --cyan: #00b4d8; --gold: #f4a21e;
      --red: #c1121f; --text: #cdd6f4; --muted: #6b7a99;
    }}
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    body {{ background:var(--dark); color:var(--text); font-family:'JetBrains Mono',monospace; overflow-x:hidden; }}
    body::before {{
      content:''; position:fixed; inset:0; pointer-events:none; z-index:0;
      background: radial-gradient(ellipse 80% 40% at 50% 0%, rgba(0,119,182,0.12) 0%, transparent 70%),
                  radial-gradient(ellipse 60% 30% at 50% 100%, rgba(0,180,216,0.07) 0%, transparent 70%);
    }}
    header {{ position:relative; z-index:10; padding:3rem 2rem 2rem; text-align:center; border-bottom:1px solid rgba(0,180,216,0.15); }}
    .logo-line {{ display:flex; align-items:center; justify-content:center; gap:1rem; margin-bottom:0.5rem; }}
    .puck {{ width:48px; height:48px; background:#111; border-radius:50%; border:3px solid var(--cyan); box-shadow:0 0 20px rgba(0,180,216,0.5); animation:pulse 3s ease-in-out infinite; }}
    @keyframes pulse {{ 0%,100% {{ box-shadow:0 0 20px rgba(0,180,216,0.5); }} 50% {{ box-shadow:0 0 40px rgba(0,180,216,0.9); }} }}
    h1 {{ font-family:'Bebas Neue',sans-serif; font-size:clamp(3rem,8vw,6rem); letter-spacing:0.08em; color:#fff; text-shadow:0 0 40px rgba(0,180,216,0.4); line-height:1; }}
    h1 span {{ color:var(--cyan); }}
    .tagline {{ margin-top:0.75rem; font-size:0.75rem; letter-spacing:0.25em; color:var(--muted); text-transform:uppercase; }}
    .meta-info {{ margin-top:0.75rem; font-size:0.7rem; color:var(--muted); }}
    .meta-info strong {{ color:var(--cyan); }}
    .version-badge {{ display:inline-block; margin-top:1rem; padding:0.25rem 0.75rem; border:1px solid var(--gold); color:var(--gold); font-size:0.7rem; letter-spacing:0.15em; border-radius:2px; }}
    .ticker {{ overflow:hidden; background:rgba(0,119,182,0.12); border-top:1px solid rgba(0,180,216,0.1); border-bottom:1px solid rgba(0,180,216,0.1); padding:0.5rem 0; margin-bottom:3rem; }}
    .ticker-inner {{ display:flex; gap:3rem; animation:ticker 25s linear infinite; white-space:nowrap; }}
    @keyframes ticker {{ from {{ transform:translateX(0); }} to {{ transform:translateX(-50%); }} }}
    .ticker-item {{ font-size:0.68rem; letter-spacing:0.1em; color:var(--muted); }}
    .ticker-item strong {{ color:var(--cyan); }}
    main {{ position:relative; z-index:10; max-width:1100px; margin:0 auto; padding:3rem 1.5rem; }}
    .section-title {{ font-family:'Bebas Neue',sans-serif; font-size:1.4rem; letter-spacing:0.12em; color:var(--cyan); margin-bottom:1.25rem; display:flex; align-items:center; gap:0.75rem; }}
    .section-title::after {{ content:''; flex:1; height:1px; background:linear-gradient(90deg, rgba(0,180,216,0.4), transparent); }}
    .matchups-bar {{ background:rgba(0,119,182,0.12); border:1px solid rgba(0,180,216,0.15); border-radius:4px; padding:1rem 1.25rem; margin-bottom:2rem; font-size:0.75rem; letter-spacing:0.08em; color:var(--text); }}
    .matchups-bar span {{ color:var(--cyan); font-weight:600; margin-right:0.5rem; }}
    .no-games {{ background:rgba(193,18,31,0.1); border:1px solid rgba(193,18,31,0.3); border-radius:4px; padding:1.25rem; margin-bottom:2rem; text-align:center; color:var(--muted); font-size:0.8rem; letter-spacing:0.1em; }}
    .table-wrap {{ overflow-x:auto; border:1px solid rgba(0,180,216,0.15); border-radius:4px; margin-bottom:3rem; }}
    table {{ width:100%; border-collapse:collapse; font-size:0.72rem; }}
    thead tr {{ background:rgba(0,119,182,0.2); }}
    th {{ padding:0.75rem 1rem; text-align:left; letter-spacing:0.12em; color:var(--cyan); font-size:0.65rem; font-weight:600; text-transform:uppercase; white-space:nowrap; }}
    tbody tr {{ border-top:1px solid rgba(0,180,216,0.07); transition:background 0.15s; }}
    tbody tr:hover {{ background:rgba(0,119,182,0.1); }}
    td {{ padding:0.7rem 1rem; color:var(--text); white-space:nowrap; }}
    td.rank {{ font-family:'Bebas Neue',sans-serif; font-size:1.1rem; color:var(--muted); width:40px; }}
    td.name {{ color:#fff; font-weight:600; }}
    td.team {{ color:var(--cyan); font-size:0.65rem; letter-spacing:0.1em; }}
    .prob-bar-wrap {{ display:flex; align-items:center; gap:0.5rem; }}
    .prob-bar {{ flex:1; height:4px; background:rgba(255,255,255,0.07); border-radius:2px; overflow:hidden; min-width:60px; }}
    .prob-fill {{ height:100%; border-radius:2px; transition:width 0.8s ease; }}
    .prob-high {{ background:linear-gradient(90deg, var(--cyan), #48cae4); }}
    .prob-mid  {{ background:linear-gradient(90deg, var(--gold), #ffc107); }}
    .prob-low  {{ background:linear-gradient(90deg, var(--red), #e63946); }}
    .prob-pct {{ font-size:0.7rem; min-width:36px; text-align:right; }}
    .over-tag {{ display:inline-block; padding:0.15rem 0.4rem; font-size:0.6rem; border-radius:2px; font-weight:600; letter-spacing:0.08em; }}
    .over-strong {{ background:rgba(0,180,216,0.2); color:var(--cyan); }}
    .over-mid    {{ background:rgba(244,162,30,0.2); color:var(--gold); }}
    .stats-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:1px; background:rgba(0,180,216,0.1); border:1px solid rgba(0,180,216,0.15); border-radius:4px; overflow:hidden; margin-bottom:3rem; }}
    .stat-card {{ background:rgba(13,27,42,0.9); padding:1.5rem; position:relative; overflow:hidden; transition:background 0.2s; }}
    .stat-card:hover {{ background:rgba(0,119,182,0.15); }}
    .stat-card::before {{ content:''; position:absolute; top:0; left:0; right:0; height:2px; background:linear-gradient(90deg, var(--cyan), transparent); }}
    .stat-label {{ font-size:0.65rem; letter-spacing:0.2em; color:var(--muted); text-transform:uppercase; margin-bottom:0.5rem; }}
    .stat-value {{ font-family:'Bebas Neue',sans-serif; font-size:2.2rem; color:#fff; letter-spacing:0.05em; }}
    .stat-unit {{ font-size:0.7rem; color:var(--cyan); margin-left:0.25rem; }}
    .glow-line {{ height:1px; background:linear-gradient(90deg, transparent, var(--cyan), transparent); margin:2.5rem 0; opacity:0.3; }}
    footer {{ position:relative; z-index:10; text-align:center; padding:2rem; border-top:1px solid rgba(0,180,216,0.1); font-size:0.65rem; color:var(--muted); letter-spacing:0.1em; }}
    footer a {{ color:var(--cyan); text-decoration:none; margin:0 0.5rem; }}
    @keyframes fadeIn {{ from {{ opacity:0; transform:translateX(-8px); }} to {{ opacity:1; transform:translateX(0); }} }}
  </style>
</head>
<body>

<header>
  <div class="logo-line">
    <div class="puck"></div>
    <h1>NHL <span>STATS</span> BOT</h1>
    <div class="puck"></div>
  </div>
  <p class="tagline">Analyse automatique · Probabilités Poisson · Top {top_n} joueurs du jour</p>
  <p class="meta-info">Mis à jour le <strong>{generated_at}</strong> · Matchs : <strong>{nb_games}</strong></p>
  <span class="version-badge">VERSION 7.0</span>
</header>

<div class="ticker">
  <div class="ticker-inner">
    <span class="ticker-item"><strong>MODE AUTO</strong> — Analyse tous les matchs NHL du jour</span>
    <span class="ticker-item"><strong>POISSON MODEL</strong> — P(Over 0.5 pts) calculé par joueur</span>
    <span class="ticker-item"><strong>15 MATCHS</strong> — Fenêtre glissante de game-logs</span>
    <span class="ticker-item"><strong>TELEGRAM</strong> — Envoi automatique du Top {top_n}</span>
    <span class="ticker-item"><strong>API NHL</strong> — Données officielles nhle.com</span>
    <span class="ticker-item"><strong>MODE AUTO</strong> — Analyse tous les matchs NHL du jour</span>
    <span class="ticker-item"><strong>POISSON MODEL</strong> — P(Over 0.5 pts) calculé par joueur</span>
    <span class="ticker-item"><strong>15 MATCHS</strong> — Fenêtre glissante de game-logs</span>
    <span class="ticker-item"><strong>TELEGRAM</strong> — Envoi automatique du Top {top_n}</span>
    <span class="ticker-item"><strong>API NHL</strong> — Données officielles nhle.com</span>
  </div>
</div>

<main>

  <div class="section-title">Matchs du jour</div>
  {no_games_banner}
  <div class="matchups-bar"><span>📅 {today}</span>{matchups}</div>

  <div class="section-title">Paramètres du modèle</div>
  <div class="stats-grid">
    <div class="stat-card"><div class="stat-label">Ligne paris</div><div class="stat-value">0.5<span class="stat-unit">PTS</span></div></div>
    <div class="stat-card"><div class="stat-label">Fenêtre matchs</div><div class="stat-value">15<span class="stat-unit">GAMES</span></div></div>
    <div class="stat-card"><div class="stat-label">TOI par défaut</div><div class="stat-value">22<span class="stat-unit">MIN</span></div></div>
    <div class="stat-card"><div class="stat-label">Top classement</div><div class="stat-value">{top_n}<span class="stat-unit">JOUEURS</span></div></div>
    <div class="stat-card"><div class="stat-label">Saison</div><div class="stat-value" style="font-size:1.4rem">2025<span class="stat-unit">/26</span></div></div>
    <div class="stat-card"><div class="stat-label">Modèle stat.</div><div class="stat-value" style="font-size:1.4rem">POISSON</div></div>
  </div>

  <div class="section-title">Top {top_n} · {today}</div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>#</th><th>Joueur</th><th>Équipe</th><th>Pos</th>
          <th>Avg PTS</th><th>TOI moy</th><th>PTS/60</th>
          <th>Expected</th><th>P(Over 0.5)</th><th>Signal</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>

  <div class="glow-line"></div>

</main>

<footer>
  <div>NHL STATS BOT · v7.0 · Python + NHL API officielle · Généré le {generated_at}</div>
  <div style="margin-top:0.5rem">
    <a href="https://github.com/Thiboulard/NHL-Stats" target="_blank">GitHub</a>
    ·
    <a href="https://api-web.nhle.com" target="_blank">NHL API</a>
    · Modèle : Poisson · Source : nhle.com
  </div>
</footer>

<script>
  setTimeout(() => {{
    document.querySelectorAll('.prob-fill').forEach(el => {{
      el.style.width = el.dataset.target + '%';
    }});
  }}, 300);
</script>

</body>
</html>""".format(
        top_n=TOP_N, generated_at=generated_at, nb_games=len(games),
        today=today, matchups=matchups if games else "Aucun match aujourd'hui",
        no_games_banner=no_games_banner, rows=rows
    )
    return html


# ============================================================
# PUSH VERS GITHUB
# ============================================================

def push_to_github(html_content):
    """Pousse index.html sur GitHub via l'API REST."""
    if not GITHUB_TOKEN:
        print("GH_TOKEN manquant — écriture locale uniquement.")
        with open("index.html", "w", encoding="utf-8") as f:
            f.write(html_content)
        return False

    api_url = "https://api.github.com/repos/{}/contents/{}".format(GITHUB_REPO, GITHUB_FILE)
    headers = {
        "Authorization": "token {}".format(GITHUB_TOKEN),
        "Accept": "application/vnd.github.v3+json",
    }

    # Récupère le SHA du fichier existant (obligatoire pour update)
    sha = None
    r = requests.get(api_url, headers=headers)
    if r.status_code == 200:
        sha = r.json().get("sha")

    content_b64 = base64.b64encode(html_content.encode("utf-8")).decode("utf-8")
    payload = {
        "message": "auto: update NHL top {} - {}".format(TOP_N, datetime.now().strftime("%Y-%m-%d %H:%M")),
        "content": content_b64,
        "branch":  GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    r2 = requests.put(api_url, headers=headers, json=payload)
    if r2.status_code in (200, 201):
        print("✅ index.html poussé sur GitHub avec succès !")
        return True
    else:
        print("❌ Erreur GitHub API : {} - {}".format(r2.status_code, r2.text[:200]))
        return False


# ============================================================
# POINT D'ENTRÉE
# ============================================================

def main():
    generated_at = datetime.now().strftime("%d/%m/%Y à %H:%M")
    print("\n" + "="*55)
    print("  NHL STATS BOT - Génération page GitHub Pages")
    print("="*55)

    # 1. Matchs du jour
    games = fetch_todays_games()
    if not games:
        print("Aucun match aujourd'hui — page mise à jour avec message 'pas de match'.")
        html = build_html([], [], generated_at)
        push_to_github(html)
        return

    # 2. Rosters
    all_players, teams_done = [], set()
    for g in games:
        for abbrev in [g["home"], g["away"]]:
            if abbrev in teams_done:
                continue
            teams_done.add(abbrev)
            print("Roster {} ...".format(abbrev))
            all_players.extend(fetch_roster_by_team_abbrev(abbrev))
            time.sleep(0.3)

    print(">>> {} joueurs à analyser".format(len(all_players)))

    # 3. Analyse
    results = []
    for i, player in enumerate(all_players):
        print("  [{}/{}] {}...".format(i+1, len(all_players), player["name"]), end="\r")
        result = analyze_player(player)
        if result:
            results.append(result)
        time.sleep(0.15)

    print("\n>>> {} joueurs analysés".format(len(results)))

    # 4. Classement
    results.sort(key=lambda x: x["p_over"], reverse=True)
    top = results[:TOP_N]

    # 5. Génère et pousse la page
    html = build_html(games, top, generated_at)
    push_to_github(html)
    print("Done ✅")


if __name__ == "__main__":
    main()
