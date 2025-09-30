# scripts/load_sleeper_players.py
from __future__ import annotations
import requests
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app import create_app, db

SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"


def fetch_all_sleeper_players() -> dict:
    r = requests.get(SLEEPER_PLAYERS_URL, timeout=90)
    r.raise_for_status()
    return r.json()  # dict keyed by sleeper_id


def ensure_indexes() -> None:
    """Create helpful indexes if they don't exist. Best-effort (ignores errors)."""
    stmts = [
        # players side
        "CREATE INDEX idx_players_mfl_id ON players (mfl_id)",
        "CREATE INDEX idx_players_name_pos ON players (name, position)",
        "CREATE INDEX idx_players_name_pos_team ON players (name, position, team)",
        # sleeper side
        "CREATE INDEX idx_sleeper_players_mfl_id ON sleeper_players (mfl_id)",
        "CREATE INDEX idx_sleeper_players_name_pos ON sleeper_players (name, position)",
        "CREATE INDEX idx_sleeper_name_pos_team ON sleeper_players (name, position, team)",
    ]
    with db.engine.begin() as conn:
        for sql in stmts:
            try:
                conn.execute(text(sql))
            except SQLAlchemyError:
                # Likely "already exists" — safe to ignore
                pass


def upsert_sleeper_rows(rows: list[dict]) -> None:
    """
    Upsert into sleeper_players:
      (sleeper_id, name, position, team, status, mfl_id)
    Preserve existing mfl_id if incoming is NULL.
    """
    sql = text("""
        INSERT INTO sleeper_players
            (sleeper_id, name, position, team, status, mfl_id)
        VALUES
            (:sleeper_id, :name, :position, :team, :status, :mfl_id)
        ON DUPLICATE KEY UPDATE
            name     = VALUES(name),
            position = VALUES(position),
            team     = VALUES(team),
            status   = VALUES(status),
            mfl_id   = COALESCE(VALUES(mfl_id), sleeper_players.mfl_id)
    """)
    with db.engine.begin() as conn:
        # Chunk to avoid oversized packets
        for i in range(0, len(rows), 1000):
            conn.execute(sql, rows[i:i+1000])


def link_exact_name_position() -> int:
    """
    Pass 1: exact name+position (case/space normalized) with DST≈DEF.
    """
    sql = text("""
        UPDATE sleeper_players sp
        JOIN players pl
          ON sp.mfl_id IS NULL
         AND UPPER(TRIM(pl.name)) = UPPER(TRIM(sp.name))
         AND UPPER(
               CASE WHEN pl.position='DST' THEN 'DEF' ELSE pl.position END
             ) = UPPER(
               CASE WHEN sp.position='DST' THEN 'DEF' ELSE sp.position END
             )
        SET sp.mfl_id = pl.mfl_id
        WHERE pl.mfl_id IS NOT NULL
    """)
    with db.engine.begin() as conn:
        res = conn.execute(sql)
        return res.rowcount


def link_lastname_position_team_regex() -> int:
    """
    Pass 2A (MySQL 8+): last_name + position + optional team, with REGEXP_REPLACE
    to drop suffixes like 'Jr.' 'Sr.' 'II' 'III' 'IV'.
    """
    sql = text(r"""
        UPDATE sleeper_players sp
        JOIN players pl
          ON sp.mfl_id IS NULL
         AND UPPER(
               CASE WHEN pl.position='DST' THEN 'DEF' ELSE pl.position END
             ) = UPPER(
               CASE WHEN sp.position='DST' THEN 'DEF' ELSE sp.position END
             )
         AND UPPER(TRIM(SUBSTRING_INDEX(
               REGEXP_REPLACE(TRIM(pl.name), ' (Jr\.|Sr\.|II|III|IV)$', ''), ' ', -1)))
           = UPPER(TRIM(SUBSTRING_INDEX(
               REGEXP_REPLACE(TRIM(sp.name), ' (Jr\.|Sr\.|II|III|IV)$', ''), ' ', -1)))
         AND (sp.team IS NULL OR pl.team IS NULL OR UPPER(pl.team) = UPPER(sp.team))
        SET sp.mfl_id = pl.mfl_id
        WHERE sp.mfl_id IS NULL
          AND pl.mfl_id IS NOT NULL
    """)
    with db.engine.begin() as conn:
        res = conn.execute(sql)
        return res.rowcount


def link_lastname_position_team_basic() -> int:
    """
    Pass 2B (MySQL 5.7-safe): last_name + position + optional team, with manual
    suffix stripping (no regex).
    """
    sql = text("""
        UPDATE sleeper_players sp
        JOIN players pl
          ON sp.mfl_id IS NULL
         AND UPPER(
               CASE WHEN pl.position='DST' THEN 'DEF' ELSE pl.position END
             ) = UPPER(
               CASE WHEN sp.position='DST' THEN 'DEF' ELSE sp.position END
             )
         AND UPPER(TRIM(SUBSTRING_INDEX(
               TRIM(REPLACE(REPLACE(REPLACE(REPLACE(pl.name,' Jr.',''),' Sr.',''),' II',''),' III','')),
               ' ', -1)))
           = UPPER(TRIM(SUBSTRING_INDEX(
               TRIM(REPLACE(REPLACE(REPLACE(REPLACE(sp.name,' Jr.',''),' Sr.',''),' II',''),' III','')),
               ' ', -1)))
         AND (sp.team IS NULL OR pl.team IS NULL OR UPPER(pl.team) = UPPER(sp.team))
        SET sp.mfl_id = pl.mfl_id
        WHERE sp.mfl_id IS NULL
          AND pl.mfl_id IS NOT NULL
    """)
    with db.engine.begin() as conn:
        res = conn.execute(sql)
        return res.rowcount


def run_linking_passes() -> dict:
    out = {"exact_name_pos": 0, "lname_pos_team": 0, "method": ""}
    out["exact_name_pos"] = link_exact_name_position()
    # Try regex version first; if it errors (e.g., MySQL 5.7), fall back.
    try:
        out["lname_pos_team"] = link_lastname_position_team_regex()
        out["method"] = "regex"
    except SQLAlchemyError:
        out["lname_pos_team"] = link_lastname_position_team_basic()
        out["method"] = "basic"
    return out


def count_linked_total() -> tuple[int, int]:
    with db.engine.begin() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM sleeper_players")).scalar_one()
        linked = conn.execute(text("SELECT COUNT(*) FROM sleeper_players WHERE mfl_id IS NOT NULL")).scalar_one()
    return total, linked


def main():
    app = create_app()
    with app.app_context():
        ensure_indexes()

        # 1) Fetch Sleeper
        data = fetch_all_sleeper_players()

        # 2) Prepare rows for upsert
        rows = []
        for sleeper_id, p in data.items():
            if not isinstance(p, dict):
                continue
            name = p.get("full_name") or p.get("name")
            if not name:
                continue
            row = {
                "sleeper_id": str(sleeper_id),
                "name": name,
                "position": p.get("position"),
                "team": p.get("team"),
                "status": p.get("status") or p.get("injury_status"),
                # Use any MFL id that Sleeper provides (some have it)
                "mfl_id": p.get("mfl_id") or (p.get("metadata") or {}).get("mfl_id"),
            }
            rows.append(row)

        # 3) Upsert into sleeper_players
        upsert_sleeper_rows(rows)

        # 4) Linking passes to fill sleeper_players.mfl_id where NULL
        link_stats = run_linking_passes()

        # 5) Report
        total, linked = count_linked_total()
        print(f"Sleeper load complete.")
        print(f"Upserted rows: {len(rows)}")
        print(f"Linking passes: exact_name_pos={link_stats['exact_name_pos']}, "
              f"lname_pos_team={link_stats['lname_pos_team']} (method={link_stats['method']})")
        print(f"sleeper_players: total={total}, linked_mfl_id={linked}, "
              f"unlinked={total - linked}")


if __name__ == "__main__":
    main()
