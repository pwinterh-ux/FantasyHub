"""Refresh Sleeper player pool and audit MFL cross-reference coverage.

Usage:
  python scripts/sync_sleeper_player_pool.py --sport nfl --limit-unmatched 50
"""
from __future__ import annotations

import argparse
from collections import Counter
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app, db
from models import Player, SleeperPlayer
from services.sleeper_client import SleeperClient


def _norm_name(name: str | None) -> str:
    if not name:
        return ""
    return " ".join(str(name).strip().lower().replace('.', '').replace("'", "").split())


def _build_mfl_index() -> dict[tuple[str, str, str], Player]:
    index: dict[tuple[str, str, str], Player] = {}
    players = Player.query.with_entities(Player.id, Player.name, Player.position, Player.team).all()
    for pid, name, pos, team in players:
        key = (_norm_name(name), (pos or "").upper(), (team or "").upper())
        # first write wins; duplicates are rare and usually stale rows
        index.setdefault(key, Player(id=pid, name=name, position=pos, team=team))
    return index


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Sleeper players and audit MFL coverage")
    parser.add_argument("--sport", default="nfl", help="Sleeper sport, defaults to nfl")
    parser.add_argument("--limit-unmatched", type=int, default=40, help="How many unmatched players to print")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        client = SleeperClient()
        catalog = client.get_players(args.sport)
        if not isinstance(catalog, dict) or not catalog:
            raise SystemExit("Sleeper catalog came back empty; aborting.")

        existing = {p.sleeper_id: p for p in SleeperPlayer.query.all()}
        touched = 0
        created = 0

        for sid, data in catalog.items():
            if not isinstance(data, dict):
                continue

            name = data.get("full_name") or data.get("search_full_name") or ""
            pos = data.get("position") or ((data.get("fantasy_positions") or [None])[0])
            team = data.get("team") or data.get("real_team")

            row = existing.get(str(sid))
            if row is None:
                row = SleeperPlayer(sleeper_id=str(sid))
                db.session.add(row)
                created += 1

            row.name = name or row.name
            row.position = (pos or row.position or "")
            row.team = (team or row.team or "")
            touched += 1

        db.session.commit()

        mfl_index = _build_mfl_index()
        sleepers = SleeperPlayer.query.with_entities(
            SleeperPlayer.sleeper_id, SleeperPlayer.name, SleeperPlayer.position, SleeperPlayer.team
        ).all()

        unmatched: list[tuple[str, str, str, str]] = []
        by_pos = Counter()

        for sid, name, pos, team in sleepers:
            key = (_norm_name(name), (pos or "").upper(), (team or "").upper())
            if not key[0] or key[1] in {"", "DEF", "K"}:
                continue
            if key not in mfl_index:
                unmatched.append((sid, name or "", pos or "", team or ""))
                by_pos[(pos or "?").upper()] += 1

        total = len(sleepers)
        matched = total - len(unmatched)
        pct = (matched / total * 100.0) if total else 0.0

        print(f"Sleeper catalog rows touched: {touched} (created {created})")
        print(f"Cross-reference coverage vs MFL players: {matched}/{total} ({pct:.2f}%)")
        if unmatched:
            print("Unmatched by position:")
            for p, count in sorted(by_pos.items(), key=lambda x: (-x[1], x[0])):
                print(f"  {p}: {count}")
            print(f"\nSample unmatched players (first {args.limit_unmatched}):")
            for sid, name, pos, team in unmatched[: args.limit_unmatched]:
                print(f"  sleeper_id={sid} | {name} | {pos} | {team}")


if __name__ == "__main__":
    main()