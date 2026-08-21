from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict

import pandas as pd
from sqlalchemy import MetaData, Table, select, text

from app import create_app, db
from models import Player, Roster


NFLVERSE_URL = (
    "https://github.com/nflverse/nflverse-data/"
    "releases/download/players/players.csv"
)

POSITIONS = {"QB", "RB", "WR", "TE"}


def clean(value):
    if value is None:
        return None

    try:
        if pd.isna(value):
            return None
    except Exception:
        pass

    s = str(value).strip()
    return s or None


def as_int(value):
    if value is None:
        return None

    try:
        if pd.isna(value):
            return None
    except Exception:
        pass

    try:
        return int(float(value))
    except Exception:
        return None


def norm(value):
    s = clean(value) or ""

    s = unicodedata.normalize("NFKD", s)
    s = "".join(
        ch for ch in s
        if not unicodedata.combining(ch)
    )

    s = s.lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
    s = re.sub(r"[^a-z0-9]", "", s)

    return s


def mfl_name_keys(name):
    raw = clean(name) or ""
    keys = set()

    if not raw:
        return keys

    keys.add(norm(raw))

    # MFL commonly stores "Last, First"
    if "," in raw:
        last, first = raw.split(",", 1)
        keys.add(norm(f"{first.strip()} {last.strip()}"))

    return {k for k in keys if k}


def nflverse_name_keys(row):
    candidates = [
        row.get("display_name"),
        row.get("football_name"),
    ]

    first = clean(row.get("first_name"))
    last = clean(row.get("last_name"))

    if first and last:
        candidates.append(f"{first} {last}")

    return {
        norm(x)
        for x in candidates
        if clean(x)
    }


def main():
    print("Downloading nflverse players...")
    df = pd.read_csv(NFLVERSE_URL, low_memory=False)

    print("NFLVERSE ROWS:", len(df))

    # ------------------------------------------------------------
    # Build nflverse name index
    # ------------------------------------------------------------
    nfl_by_name = defaultdict(list)

    for idx, row in df.iterrows():
        data = row.to_dict()

        for key in nflverse_name_keys(data):
            nfl_by_name[key].append(data)

    app = create_app()

    with app.app_context():

        # --------------------------------------------------------
        # Existing draft metadata
        # --------------------------------------------------------
        existing_meta = {
            str(row[0])
            for row in db.session.execute(
                text("SELECT mfl_id FROM player_draft_meta")
            ).all()
        }

        # --------------------------------------------------------
        # Canonical rostered MFL IDs
        # --------------------------------------------------------
        rostered_ids = set()

        mfl_rows = (
            db.session.query(Player.mfl_id)
            .join(Roster, Roster.player_id == Player.id)
            .filter(Player.position.in_(POSITIONS))
            .distinct()
            .all()
        )

        for (mid,) in mfl_rows:
            if mid not in (None, ""):
                rostered_ids.add(str(mid))

        # Sleeper mapped rostered IDs
        md = MetaData()
        engine = db.session.get_bind()

        sleeper_players = Table(
            "sleeper_players",
            md,
            autoload_with=engine,
        )

        sleeper_rosters = Table(
            "sleeper_rosters",
            md,
            autoload_with=engine,
        )

        q = (
            select(
                sleeper_players.c.mfl_id
            )
            .select_from(
                sleeper_rosters.join(
                    sleeper_players,
                    sleeper_rosters.c.player_sid
                    == sleeper_players.c.sleeper_id,
                )
            )
            .where(
                sleeper_players.c.position.in_(POSITIONS)
            )
            .distinct()
        )

        for row in db.session.execute(q):
            mid = row[0]

            if mid not in (None, ""):
                rostered_ids.add(str(mid))

        missing_ids = sorted(rostered_ids - existing_meta)

        print("Rostered canonical players:", len(rostered_ids))
        print("Already have draft meta:", len(rostered_ids & existing_meta))
        print("Need backfill:", len(missing_ids))

        players = (
            Player.query
            .filter(Player.mfl_id.in_(missing_ids))
            .all()
        )

        player_by_mid = {
            str(p.mfl_id): p
            for p in players
        }

        matched = []
        unmatched = []
        ambiguous = []
        no_class = []

        for mid in missing_ids:
            p = player_by_mid.get(mid)

            if not p:
                unmatched.append({
                    "mfl_id": mid,
                    "name": "(not in Player ORM query)",
                    "position": None,
                    "team": None,
                    "reason": "missing_player_row",
                })
                continue

            candidates = []

            seen = set()

            for key in mfl_name_keys(p.name):
                for cand in nfl_by_name.get(key, []):
                    # Deduplicate repeated index hits.
                    identity = (
                        clean(cand.get("gsis_id")),
                        clean(cand.get("display_name")),
                        clean(cand.get("birth_date")),
                    )

                    if identity not in seen:
                        seen.add(identity)
                        candidates.append(cand)

            if len(candidates) > 1:
                # Position can break ties, but is NOT required.
                same_pos = [
                    c for c in candidates
                    if clean(c.get("position")) == p.position
                ]

                if len(same_pos) == 1:
                    candidates = same_pos

            if len(candidates) == 0:
                unmatched.append({
                    "mfl_id": mid,
                    "name": p.name,
                    "position": p.position,
                    "team": p.team,
                    "reason": "no_name_match",
                })
                continue

            if len(candidates) > 1:
                ambiguous.append({
                    "mfl_id": mid,
                    "name": p.name,
                    "position": p.position,
                    "team": p.team,
                    "candidates": " || ".join(
                        str(clean(c.get("display_name")))
                        for c in candidates
                    ),
                })
                continue

            c = candidates[0]

            draft_year = as_int(c.get("draft_year"))
            rookie_season = as_int(c.get("rookie_season"))

            if draft_year is not None:
                class_year = draft_year
                is_udfa = False
                draft_round = as_int(c.get("draft_round"))
                draft_pick = as_int(c.get("draft_pick"))
                draft_team = clean(c.get("draft_team"))

            elif rookie_season is not None:
                class_year = rookie_season
                is_udfa = True
                draft_round = None
                draft_pick = None
                draft_team = None

            else:
                no_class.append({
                    "mfl_id": mid,
                    "name": p.name,
                    "position": p.position,
                    "team": p.team,
                    "nflverse_name": clean(c.get("display_name")),
                })
                continue

            matched.append({
                "mfl_id": mid,
                "name": p.name,
                "position": p.position,
                "team": p.team,
                "nflverse_name": clean(c.get("display_name")),
                "draft_year": class_year,
                "draft_round": draft_round,
                "draft_pick": draft_pick,
                "draft_team": draft_team,
                "is_udfa": int(is_udfa),
                "source": "nflverse",
            })

        matched_df = pd.DataFrame(matched)

        if not matched_df.empty:
            matched_df = matched_df.sort_values(
                ["draft_year", "draft_pick", "name"],
                ascending=[False, True, True],
                na_position="last",
            )

        matched_df.to_csv(
            "data/draft_meta_nflverse_backfill.csv",
            index=False,
        )

        pd.DataFrame(unmatched).to_csv(
            "data/draft_meta_nflverse_unmatched.csv",
            index=False,
        )

        pd.DataFrame(ambiguous).to_csv(
            "data/draft_meta_nflverse_ambiguous.csv",
            index=False,
        )

        pd.DataFrame(no_class).to_csv(
            "data/draft_meta_nflverse_noclass.csv",
            index=False,
        )

        drafted = sum(
            1 for x in matched
            if not x["is_udfa"]
        )

        udfa = sum(
            1 for x in matched
            if x["is_udfa"]
        )

        print()
        print("=" * 76)
        print("NFLVERSE BACKFILL RESULT")
        print("=" * 76)
        print("Missing before backfill:", len(missing_ids))
        print("Matched:", len(matched))
        print("  Drafted:", drafted)
        print("  UDFA:", udfa)
        print("Unmatched:", len(unmatched))
        print("Ambiguous:", len(ambiguous))
        print("Matched but no rookie/draft year:", len(no_class))
        print()
        print("CSV: data/draft_meta_nflverse_backfill.csv")

        if unmatched:
            print()
            print("UNMATCHED")
            print("-" * 76)

            for x in unmatched:
                print(
                    x["mfl_id"],
                    "|", x["position"],
                    "|", x["name"],
                    "|", x["reason"],
                )

        if ambiguous:
            print()
            print("AMBIGUOUS")
            print("-" * 76)

            for x in ambiguous:
                print(
                    x["mfl_id"],
                    "|", x["position"],
                    "|", x["name"],
                    "=>", x["candidates"],
                )

        if no_class:
            print()
            print("MATCHED BUT NO CLASS YEAR")
            print("-" * 76)

            for x in no_class:
                print(
                    x["mfl_id"],
                    "|", x["position"],
                    "|", x["name"],
                    "=>", x["nflverse_name"],
                )


if __name__ == "__main__":
    main()
