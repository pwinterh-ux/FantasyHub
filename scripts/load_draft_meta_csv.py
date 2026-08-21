from __future__ import annotations

import argparse
import csv
from pathlib import Path

from sqlalchemy import text

from app import create_app, db


CREATE_SQL = """
CREATE TABLE IF NOT EXISTS player_draft_meta (
    mfl_id VARCHAR(20) NOT NULL PRIMARY KEY,
    draft_year INT NOT NULL,
    draft_round INT NULL,
    draft_pick INT NULL,
    draft_team VARCHAR(80) NULL,
    is_udfa BOOLEAN NOT NULL DEFAULT FALSE,
    source VARCHAR(32) NOT NULL DEFAULT 'wikipedia',
    manual_override BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    INDEX ix_player_draft_meta_year (draft_year),
    INDEX ix_player_draft_meta_pick (draft_year, draft_pick)
)
"""

UPSERT_SQL = """
INSERT INTO player_draft_meta (
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
    FALSE,
    'wikipedia',
    FALSE
)
ON DUPLICATE KEY UPDATE
    draft_year = IF(
        manual_override,
        draft_year,
        VALUES(draft_year)
    ),
    draft_round = IF(
        manual_override,
        draft_round,
        VALUES(draft_round)
    ),
    draft_pick = IF(
        manual_override,
        draft_pick,
        VALUES(draft_pick)
    ),
    draft_team = IF(
        manual_override,
        draft_team,
        VALUES(draft_team)
    ),
    is_udfa = IF(
        manual_override,
        is_udfa,
        FALSE
    ),
    source = IF(
        manual_override,
        source,
        'wikipedia'
    )
"""


def as_int(value):
    if value in (None, ""):
        return None
    return int(value)


def read_rows(start_year: int, end_year: int):
    all_rows = []
    counts = {}
    missing = []

    for year in range(start_year, end_year + 1):
        path = Path("data") / f"draft_meta_{year}.csv"

        if not path.exists():
            missing.append(str(path))
            continue

        rows = []

        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)

            required = {
                "mfl_id",
                "draft_year",
                "draft_round",
                "draft_pick",
                "draft_team",
                "name",
                "position",
            }

            missing_cols = required - set(reader.fieldnames or [])

            if missing_cols:
                raise RuntimeError(
                    f"{path}: missing columns {sorted(missing_cols)}"
                )

            for raw in reader:
                row = {
                    "mfl_id": str(raw["mfl_id"]).strip(),
                    "draft_year": as_int(raw["draft_year"]),
                    "draft_round": as_int(raw["draft_round"]),
                    "draft_pick": as_int(raw["draft_pick"]),
                    "draft_team": (raw.get("draft_team") or "").strip() or None,
                    "name": (raw.get("name") or "").strip(),
                    "position": (raw.get("position") or "").strip(),
                    "file": str(path),
                }

                if row["draft_year"] != year:
                    raise RuntimeError(
                        f"{path}: row has draft_year={row['draft_year']}"
                    )

                if not row["mfl_id"]:
                    raise RuntimeError(
                        f"{path}: blank MFL ID for {row['name']}"
                    )

                rows.append(row)

        counts[year] = len(rows)
        all_rows.extend(rows)

    if missing:
        raise RuntimeError(
            "Missing CSV files:\n  " + "\n  ".join(missing)
        )

    return all_rows, counts


def validate(rows):
    by_mfl = {}
    by_pick = {}

    duplicate_mfl = []
    duplicate_picks = []

    for row in rows:
        mid = row["mfl_id"]

        if mid in by_mfl:
            old = by_mfl[mid]

            duplicate_mfl.append(
                (
                    mid,
                    old["draft_year"],
                    old["name"],
                    row["draft_year"],
                    row["name"],
                )
            )
        else:
            by_mfl[mid] = row

        if row["draft_pick"] is not None:
            key = (
                row["draft_year"],
                row["draft_pick"],
            )

            if key in by_pick:
                old = by_pick[key]

                duplicate_picks.append(
                    (
                        key,
                        old["name"],
                        row["name"],
                    )
                )
            else:
                by_pick[key] = row

    if duplicate_mfl:
        print()
        print("CONFLICT: SAME MFL ID IN MULTIPLE CSV ROWS")

        for x in duplicate_mfl:
            print(
                "MFL", x[0],
                "|", x[1], x[2],
                "|", x[3], x[4],
            )

    if duplicate_picks:
        print()
        print("CONFLICT: DUPLICATE YEAR/PICK")

        for key, a, b in duplicate_picks:
            print(
                key[0],
                "pick", key[1],
                "|", a,
                "|", b,
            )

    if duplicate_mfl or duplicate_picks:
        raise RuntimeError(
            "Preflight conflicts found. Database not changed."
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=2014)
    parser.add_argument("--end", type=int, default=2026)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    rows, counts = read_rows(args.start, args.end)
    validate(rows)

    print()
    print("=" * 72)
    print("DRAFT META LOAD PREFLIGHT")
    print("=" * 72)

    for year in sorted(counts, reverse=True):
        print(year, ":", counts[year])

    print("-" * 72)
    print("TOTAL:", len(rows))

    if not args.commit:
        print()
        print("DRY RUN ONLY - database was not changed.")
        print("Preflight passed.")
        return

    app = create_app()

    with app.app_context():
        db.session.execute(text(CREATE_SQL))

        # Verify every imported MFL id still exists in players.
        imported_ids = {r["mfl_id"] for r in rows}

        existing_ids = {
            str(r[0])
            for r in db.session.execute(
                text("""
                    SELECT mfl_id
                    FROM players
                    WHERE mfl_id IN :ids
                """).bindparams(
                    __import__("sqlalchemy").bindparam(
                        "ids",
                        expanding=True
                    )
                ),
                {"ids": sorted(imported_ids)},
            )
        }

        nonexistent = sorted(imported_ids - existing_ids)

        if nonexistent:
            db.session.rollback()
            raise RuntimeError(
                "CSV contains MFL IDs not present in players: "
                + ", ".join(nonexistent)
            )

        for row in rows:
            db.session.execute(
                text(UPSERT_SQL),
                {
                    "mfl_id": row["mfl_id"],
                    "draft_year": row["draft_year"],
                    "draft_round": row["draft_round"],
                    "draft_pick": row["draft_pick"],
                    "draft_team": row["draft_team"],
                },
            )

        db.session.commit()

        total = db.session.execute(
            text("SELECT COUNT(*) FROM player_draft_meta")
        ).scalar()

        print()
        print("COMMIT SUCCESSFUL")
        print("Rows submitted:", len(rows))
        print("Rows now in player_draft_meta:", total)


if __name__ == "__main__":
    main()
