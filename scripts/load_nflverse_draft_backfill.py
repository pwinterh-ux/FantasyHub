from __future__ import annotations

import csv
from pathlib import Path

from sqlalchemy import text

from app import create_app, db


CSV_PATH = Path("data/draft_meta_nflverse_backfill.csv")

INSERT_SQL = """
INSERT IGNORE INTO player_draft_meta (
    mfl_id,
    draft_year,
    draft_round,
    draft_pick,
    draft_team,
    is_udfa,
    source,
    manual_override
)
VALUES (
    :mfl_id,
    :draft_year,
    :draft_round,
    :draft_pick,
    :draft_team,
    :is_udfa,
    'nflverse',
    FALSE
)
"""


def as_int(value):
    if value in (None, ""):
        return None

    s = str(value).strip()

    if not s or s.lower() == "nan":
        return None

    return int(float(s))


def as_bool(value):
    return str(value or "").strip().lower() in {
        "1", "true", "yes"
    }


def main():
    if not CSV_PATH.exists():
        raise SystemExit(f"Missing: {CSV_PATH}")

    rows = []

    with CSV_PATH.open(
        newline="",
        encoding="utf-8",
    ) as f:
        reader = csv.DictReader(f)

        for raw in reader:
            rows.append({
                "mfl_id": str(raw["mfl_id"]).strip(),
                "draft_year": as_int(raw["draft_year"]),
                "draft_round": as_int(raw.get("draft_round")),
                "draft_pick": as_int(raw.get("draft_pick")),
                "draft_team": (
                    str(raw.get("draft_team") or "").strip()
                    or None
                ),
                "is_udfa": as_bool(raw.get("is_udfa")),
            })

    print("CSV rows:", len(rows))

    app = create_app()

    with app.app_context():

        before = db.session.execute(
            text("SELECT COUNT(*) FROM player_draft_meta")
        ).scalar()

        print("Before:", before)

        for row in rows:
            db.session.execute(
                text(INSERT_SQL),
                row,
            )

        db.session.commit()

        after = db.session.execute(
            text("SELECT COUNT(*) FROM player_draft_meta")
        ).scalar()

        nflverse = db.session.execute(
            text("""
                SELECT COUNT(*)
                FROM player_draft_meta
                WHERE source = 'nflverse'
            """)
        ).scalar()

        udfa = db.session.execute(
            text("""
                SELECT COUNT(*)
                FROM player_draft_meta
                WHERE is_udfa = TRUE
            """)
        ).scalar()

        print("After:", after)
        print("Inserted:", after - before)
        print("NFLVERSE:", nflverse)
        print("UDFA:", udfa)


if __name__ == "__main__":
    main()
