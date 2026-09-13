"""Read-only, request-independent lineup checker decision engine."""
from __future__ import annotations

import concurrent.futures
import xml.etree.ElementTree as ET
from typing import Callable

import requests

from services.lineups_service import Projection, fetch_projected_scores, parse_lineup_requirements
from services.lineup_constraints import lineup_satisfies_constraints
from services.nfl_schedule_service import BYE, LOCKED, NO_GAME, UNKNOWN, game_state_for_team
from services.lineup_lock_service import fetch_player_roster_statuses

LINEUP_CHECK_MIN_GAIN = 2.0
CLASS_PRIORITY = {"CRITICAL": 0, "ACTION": 1, "WATCH": 2, "ERROR": 3, "GOOD": 4}
UNAVAILABLE = ("out", "suspended", "ir", "pup")


def fetch_injuries(year: int, week: int, *, timeout: int = 20) -> dict[int, dict]:
    response = requests.get(f"https://api.myfantasyleague.com/{year}/export",
                            params={"TYPE": "injuries", "W": str(week), "JSON": "0"}, timeout=timeout)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    return {int(n.get("id")): {"status": n.get("status", ""), "details": n.get("details", ""),
                               "exp_return": n.get("exp_return", "")}
            for n in root.findall(".//injury") if str(n.get("id", "")).isdigit()}


def build_constrained_optimal_lineup(players: list[dict], total: int | None, ranges: dict,
                                     frozen_ids: set[int], forbidden_ids: set[int]) -> dict:
    """Dynamic programming optimizer; never invents a lineup when constraints fail."""
    by_id = {int(p["player_id"]): p for p in players}
    frozen = [by_id[x] for x in sorted(frozen_ids) if x in by_id]
    target = total if total is not None else sum(v[1] for v in ranges.values())
    if target is None: target = len(frozen)
    counts = {}
    for p in frozen: counts[p["position"]] = counts.get(p["position"], 0) + 1
    if len(frozen) > target or not lineup_satisfies_constraints(
            counts, ranges, minimums=False):
        return {"ok": False, "reason": "Frozen starters violate lineup limits", "starter_ids": []}
    candidates = [p for p in players if int(p["player_id"]) not in frozen_ids | forbidden_ids
                  and p.get("projection") is not None]
    candidates.sort(key=lambda p: (-float(p["projection"]), int(p["player_id"])))
    # state=(number, sorted counts), value=(score, ids)
    initial_key = (len(frozen), tuple(sorted(counts.items())))
    dp = {initial_key: (sum(float(p.get("projection") or 0) for p in frozen), tuple(sorted(frozen_ids)))}
    for p in candidates:
        pos, pid, score = p["position"], int(p["player_id"]), float(p["projection"])
        for key, value in list(dp.items()):
            n, count_tuple = key
            current = dict(count_tuple)
            if n >= target: continue
            current[pos] = current.get(pos, 0) + 1
            if not lineup_satisfies_constraints(current, ranges, minimums=False):
                continue
            newkey = (n + 1, tuple(sorted(current.items())))
            newval = (value[0] + score, tuple(sorted(value[1] + (pid,))))
            if newkey not in dp or newval[0] > dp[newkey][0] or (newval[0] == dp[newkey][0] and newval[1] < dp[newkey][1]):
                dp[newkey] = newval
    valid = [(v, dict(k[1])) for k, v in dp.items() if k[0] == target and
             lineup_satisfies_constraints(dict(k[1]), ranges)]
    if not valid: return {"ok": False, "reason": "No legal projected lineup satisfies requirements", "starter_ids": []}
    best = max(valid, key=lambda x: (x[0][0], tuple(-i for i in x[0][1])))[0]
    return {"ok": True, "reason": None, "starter_ids": list(best[1])}


def _injury_kind(status: str) -> str:
    value = status.strip().lower()
    if value == "o": return "UNAVAILABLE"
    if any(value == x or value.startswith(x + " ") or x in value for x in UNAVAILABLE): return "UNAVAILABLE"
    if value in {"questionable", "q", "doubtful", "d"}: return "WATCH"
    return "HEALTHY"


def check_league_lineup(job: dict, *, week: int, injuries: dict[int, dict], injuries_ok: bool,
                        game_states: dict, schedule_ok: bool, week_complete: bool,
                        roster_statuses: dict[int, str], projections: dict[int, Projection]) -> dict:
    players = []
    findings = []
    current = {pid for pid, status in roster_statuses.items() if status == "S"}
    total, ranges = parse_lineup_requirements(job.get("roster_slots"))
    if not current:
        findings.append({"type": "NO_LINEUP", "severity": "CRITICAL", "message": "No lineup currently submitted"})
    elif total is not None and len(current) < total:
        findings.append({"type": "INCOMPLETE_LINEUP", "severity": "CRITICAL",
                         "message": f"Current lineup has {len(current)} of {total} required starters."})
    elif total is not None and len(current) > total:
        findings.append({"type": "INVALID_LINEUP_COUNT", "severity": "CRITICAL",
                         "message": f"MFL reports {len(current)} starters but this league requires {total}; recommendations are disabled."})
    frozen, forbidden = set(), set()
    for raw in job["players"]:
        p = dict(raw); pid = int(p["player_id"])
        p["projection"] = projections.get(pid).projected if projections.get(pid) else None
        p["game"] = game_state_for_team(p.get("team"), game_states, schedule_verified=schedule_ok, week_complete=week_complete)
        injury = injuries.get(pid, {}); kind = _injury_kind(injury.get("status", "")) if injuries_ok else "UNKNOWN"
        p["injury"] = injury; players.append(p)
        is_starter = pid in current
        if is_starter and p["game"]["state"] in {LOCKED, UNKNOWN}: frozen.add(pid)
        if (not is_starter and p["game"]["state"] in {LOCKED, UNKNOWN}) or p["game"]["state"] in {BYE, NO_GAME} or p.get("roster_status") != "ACTIVE" or kind == "UNAVAILABLE": forbidden.add(pid)
        if is_starter and (kind == "UNAVAILABLE" or p["game"]["state"] in {BYE, NO_GAME} or p.get("roster_status") != "ACTIVE"):
            finding_type = ("BYE_STARTER" if p["game"]["state"] == BYE else
                            "NO_GAME_STARTER" if p["game"]["state"] == NO_GAME else
                            "UNAVAILABLE_STARTER")
            findings.append({"type": finding_type,
                             "severity": "CRITICAL", "player_id": pid, "player_name": p.get("name"),
                             "injury_status": injury.get("status"), "game_state": p["game"]["state"],
                             "message": "No lineup change is possible for this player." if pid in frozen else "Starter needs attention"})
        elif is_starter and kind == "WATCH":
            findings.append({"type": "INJURY_WATCH", "severity": "WATCH", "player_id": pid,
                             "player_name": p.get("name"), "injury_status": injury.get("status"), "game_state": p["game"]["state"]})
        # Missing data is not evidence that a healthy/watch starter is worse.
        # Definite unavailable/bye/illegal starters remain repairable.
        if is_starter and p["projection"] is None and kind != "UNAVAILABLE" and \
                p["game"]["state"] not in {BYE, NO_GAME} and p.get("roster_status") == "ACTIVE":
            frozen.add(pid)
    # A stale schedule cannot prove that a starter may leave or a bench player may
    # enter.  A failed injury feed likewise cannot prove a candidate is available.
    if not schedule_ok or not injuries_ok:
        frozen.update(current)
        forbidden.update(int(p["player_id"]) for p in players if int(p["player_id"]) not in current)
    if total is not None and len(current) > total:
        frozen.update(current)
        forbidden.update(int(p["player_id"]) for p in players if int(p["player_id"]) not in current)
    optimal = build_constrained_optimal_lineup(players, total, ranges, frozen, forbidden)
    recommended = set(optimal["starter_ids"]) if optimal["ok"] else set(current)
    leaving, entering = sorted(current - recommended), sorted(recommended - current)
    def known_total(ids):
        vals = [next((p["projection"] for p in players if p["player_id"] == pid), None) for pid in ids]
        return round(sum(vals), 2) if vals and all(v is not None for v in vals) else None
    cur_total, rec_total = known_total(current), known_total(recommended)
    gain = round(rec_total-cur_total, 2) if cur_total is not None and rec_total is not None else None
    if schedule_ok and injuries_ok and gain is not None and gain >= LINEUP_CHECK_MIN_GAIN and entering:
        findings.append({"type": "PROJECTION_UPGRADE", "severity": "ACTION", "projected_gain": gain})
    if not injuries_ok: findings.append({"type": "INJURY_DATA_ERROR", "severity": "ERROR", "message": "Current injury report unavailable"})
    if not schedule_ok: findings.append({"type": "SCHEDULE_ERROR", "severity": "ERROR", "message": "Game lock status unavailable; swap recommendations disabled"})
    if not optimal["ok"]: findings.append({"type": "CONSTRAINT_ERROR", "severity": "ERROR", "message": optimal["reason"]})
    classification = min((f["severity"] for f in findings), key=lambda x: CLASS_PRIORITY[x], default="GOOD")
    display_by_id = {int(p["player_id"]): {"player_id": int(p["player_id"]), "name": p.get("name"),
                     "position": p.get("position"), "team": p.get("team"), "projection": p.get("projection")}
                     for p in players}
    return {"league_id": job["league_id"], "league_name": job["league_name"], "classification": classification,
            "current_starter_ids": sorted(current), "recommended_starter_ids": sorted(recommended),
            "leaving_player_ids": leaving, "entering_player_ids": entering,
            "current_projected_total": cur_total, "recommended_projected_total": rec_total,
            "projected_gain": gain, "findings": findings, "players": players,
            "leaving_players": [display_by_id[x] for x in leaving],
            "entering_players": [display_by_id[x] for x in entering]}


def check_user_lineups(jobs: list[dict], *, season: int, week: int, injuries: dict,
                       injuries_ok: bool, game_states: dict, schedule_ok: bool, week_complete: bool,
                       max_workers: int = 3,
                       status_fetcher: Callable = fetch_player_roster_statuses,
                       projection_fetcher: Callable = fetch_projected_scores) -> dict:
    """Network workers consume primitive snapshots and failures remain per-league."""
    manual = [j for j in jobs if not j.get("best_ball")]
    def fetch(job):
        statuses = status_fetcher(job, week)
        projections = projection_fetcher(job["host"], job["mfl_id"], job["year"], week,
                                         job["player_ids"], cookie=job.get("cookie"))
        return statuses, projections
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch, j): j for j in manual if not j.get("refresh_error")}
        net = {}
        for future, job in futures.items():
            try: net[job["league_id"]] = future.result()
            except Exception as exc: net[job["league_id"]] = exc
    for job in manual:
        problem = job.get("refresh_error") or net.get(job["league_id"])
        if isinstance(problem, (Exception, str)):
            results.append({"league_id": job["league_id"], "league_name": job["league_name"],
                            "classification": "ERROR", "findings": [{"type": "LEAGUE_DATA_ERROR", "severity": "ERROR", "message": str(problem)}]})
            continue
        statuses, projections = problem
        results.append(check_league_lineup(job, week=week, injuries=injuries, injuries_ok=injuries_ok,
            game_states=game_states, schedule_ok=schedule_ok, week_complete=week_complete,
            roster_statuses=statuses, projections=projections))
    summary = {k: sum(x["classification"] == k.upper() for x in results) for k in ("critical", "action", "watch", "error", "good")}
    summary["best_ball_skipped"] = len(jobs)-len(manual)
    results.sort(key=lambda x: (CLASS_PRIORITY[x["classification"]], x["league_name"].lower()))
    return {"season": season, "week": week, "schedule_ok": schedule_ok, "injuries_ok": injuries_ok,
            "summary": summary, "leagues": results}
