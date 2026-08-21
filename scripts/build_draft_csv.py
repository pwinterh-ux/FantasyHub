from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

from app import create_app
from models import Player
from rankings.sources.fantasycalc import normalize_name_for_matching


FANTASY_POSITIONS = {"QB", "RB", "WR", "TE"}

# Explicit draft-source -> MFL identity overrides.
# Used only when normal name+position matching finds no candidate.
MANUAL_MFL_OVERRIDES = {
    (2026, normalize_name_for_matching("CJ Williams"), "WR"): "17671",

    # 2024
    (2024, normalize_name_for_matching("Audric Estimé"), "RB"): "16595",

    # 2022
    (2022, normalize_name_for_matching("Chig Okonkwo"), "TE"): "15889",

    # 2021
    (2021, normalize_name_for_matching("Rondale Moore"), "WR"): "15283",
    (2021, normalize_name_for_matching("D'Wayne Eskridge"), "WR"): "15302",
    (2021, normalize_name_for_matching("Josh Palmer"), "WR"): "15319",
    (2020, normalize_name_for_matching("A. J. Dillon"), "RB"): "14805",
    (2020, normalize_name_for_matching("Gabe Davis"), "WR"): "14845",
    (2020, normalize_name_for_matching("La'Mical Perine"), "RB"): "14806",
}


def clean(value):
    if value is None:
        return ""
    s = str(value).strip()
    s = re.sub(r"\[[^\]]+\]", "", s)
    s = s.replace("†", "").replace("‡", "").replace("*", "")
    return " ".join(s.split())


def to_int(value):
    s = clean(value)
    m = re.search(r"\d+", s)
    return int(m.group()) if m else None


def flatten_column(col):
    if isinstance(col, tuple):
        return " ".join(
            clean(x)
            for x in col
            if clean(x) and not clean(x).lower().startswith("unnamed")
        )
    return clean(col)


def find_col(columns, *terms):
    for col in columns:
        low = col.lower()
        if any(term.lower() in low for term in terms):
            return col
    return None


def mfl_name_keys(name):
    raw = (name or "").strip()
    keys = set()

    if not raw:
        return keys

    norm = normalize_name_for_matching(raw)
    if norm:
        keys.add(norm)

    if "," in raw:
        last, first = [x.strip() for x in raw.split(",", 1)]
        if first and last:
            norm = normalize_name_for_matching(f"{first} {last}")
            if norm:
                keys.add(norm)

    return keys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, required=True)
    args = parser.parse_args()

    year = args.year
    url = f"https://en.wikipedia.org/wiki/{year}_NFL_draft"

    print("Fetching:", url)

    tables = pd.read_html(
        url,
        storage_options={"User-Agent": "RosterDash draft loader"},
    )

    draft = None

    for df in tables:
        df = df.copy()
        df.columns = [flatten_column(c) for c in df.columns]
        blob = " | ".join(df.columns).lower()

        if (
            "player" in blob
            and "pick" in blob
            and ("rnd" in blob or "round" in blob)
            and ("pos" in blob or "position" in blob)
        ):
            draft = df
            break

    if draft is None:
        raise RuntimeError("Could not find NFL draft table.")

    cols = list(draft.columns)

    player_col = find_col(cols, "player")
    round_col = find_col(cols, "rnd", "round")
    pick_col = find_col(cols, "pick")
    team_col = find_col(cols, "team", "tm")
    pos_col = find_col(cols, "pos", "position")

    print("Columns:")
    print(" player:", player_col)
    print(" round :", round_col)
    print(" pick  :", pick_col)
    print(" team  :", team_col)
    print(" pos   :", pos_col)

    source_rows = []

    for _, row in draft.iterrows():
        name = clean(row[player_col])
        pos = clean(row[pos_col]).upper()
        rnd = to_int(row[round_col])
        pick = to_int(row[pick_col])

        if pos not in FANTASY_POSITIONS:
            continue

        if not name or rnd is None or pick is None:
            continue

        source_rows.append({
            "draft_year": year,
            "draft_round": rnd,
            "draft_pick": pick,
            "draft_team": clean(row[team_col]),
            "name": name,
            "position": pos,
        })

    app = create_app()

    with app.app_context():
        by_key = {}

        for p in Player.query.filter(
            Player.position.in_(FANTASY_POSITIONS)
        ).all():
            pos = str(p.position or "").upper()

            for key in mfl_name_keys(p.name):
                by_key.setdefault((key, pos), []).append(p)

        matched = []
        unmatched = []
        ambiguous = []

        for row in source_rows:
            norm = normalize_name_for_matching(row["name"])
            candidates = by_key.get(
                (norm, row["position"]),
                [],
            )

            # Handle known source/MFL naming aliases.
            override_id = MANUAL_MFL_OVERRIDES.get(
                (year, norm, row["position"])
            )

            if not candidates and override_id:
                override_player = Player.query.filter_by(
                    mfl_id=override_id
                ).first()

                if override_player:
                    candidates = [override_player]

            if len(candidates) == 1:
                p = candidates[0]

                matched.append({
                    **row,
                    "mfl_id": str(p.mfl_id),
                    "mfl_name": p.name,
                })

            elif len(candidates) == 0:
                unmatched.append(row)

            else:
                ambiguous.append((row, candidates))

        print()
        print("=" * 80)
        print("RESULT")
        print("=" * 80)
        print("Fantasy draft picks:", len(source_rows))
        print("Matched:", len(matched))
        print("Unmatched:", len(unmatched))
        print("Ambiguous:", len(ambiguous))

        print()
        print("UNMATCHED")
        print("=" * 80)

        if not unmatched:
            print("NONE")
        else:
            for r in unmatched:
                print(
                    r["draft_pick"],
                    "| R" + str(r["draft_round"]),
                    "|", r["position"],
                    "|", r["name"],
                )

        print()
        print("AMBIGUOUS")
        print("=" * 80)

        if not ambiguous:
            print("NONE")
        else:
            for r, candidates in ambiguous:
                print(
                    r["draft_pick"],
                    r["position"],
                    r["name"],
                )
                for p in candidates:
                    print(
                        "  MFL",
                        p.mfl_id,
                        "|", p.name,
                        "|", p.team,
                    )

        outfile = Path("data") / f"draft_meta_{year}.csv"

        pd.DataFrame(matched).sort_values(
            "draft_pick"
        ).to_csv(outfile, index=False)

        print()
        print("CSV WRITTEN:", outfile)
        print("ROWS:", len(matched))


if __name__ == "__main__":
    main()
