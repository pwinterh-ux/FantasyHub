"""CLI runner for dynasty rank refresh (source + consensus + audit)."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime

from app import create_app, db
from models import DynastyRankConsensusCurrent, DynastyRankIngestAudit, DynastyRankSourceCurrent
from rankings.mfl_map import adapt_fantasycalc_rows, adapt_fantasypros_rows, adapt_keeptradecut_rows, map_rows_to_mfl
from rankings.sources.fantasycalc import fetch_and_rank_dynasty_values
from rankings.sources.keeptradecut import fetch_positional_rankings
from rankings.sources.fantasypros import fetch_fantasypros_dynasty_trade_values


MISSING_SOURCE_RANK_PENALTY = 5


def _upsert_source_rows(rows):
    by_key = {(r.source, r.position, r.mfl_id): r for r in rows}
    if not by_key:
        return 0

    keys = list(by_key.keys())
    existing = DynastyRankSourceCurrent.query.filter(
        DynastyRankSourceCurrent.source.in_([k[0] for k in keys]),
        DynastyRankSourceCurrent.position.in_([k[1] for k in keys]),
        DynastyRankSourceCurrent.mfl_id.in_([k[2] for k in keys]),
    ).all()
    existing_map = {(e.source, e.position, e.mfl_id): e for e in existing}

    now = datetime.utcnow()
    writes = 0
    for key, row in by_key.items():
        rec = existing_map.get(key)
        if rec is None:
            rec = DynastyRankSourceCurrent(source=row.source, position=row.position, mfl_id=row.mfl_id)
            db.session.add(rec)

        rec.player_name = row.player_name
        rec.source_rank = row.source_rank
        rec.source_value = row.source_value
        rec.updated_at_utc = now
        writes += 1

    return writes


def _recompute_consensus_current() -> int:
    now = datetime.utcnow()
    source_rows = DynastyRankSourceCurrent.query.all()
    grouped: dict[tuple[str, str], list[DynastyRankSourceCurrent]] = defaultdict(list)

    # Build per-position source metadata for missing-source penalties.
    max_rank_by_position_source: dict[tuple[str, str], int] = {}
    sources_by_position: dict[str, set[str]] = defaultdict(set)

    for row in source_rows:
        position = str(row.position).upper()
        source = str(row.source).lower()
        grouped[(position, row.mfl_id)].append(row)
        sources_by_position[position].add(source)

        rank_i = int(row.source_rank) if row.source_rank is not None else None
        if rank_i is not None:
            key = (position, source)
            prev = max_rank_by_position_source.get(key, 0)
            if rank_i > prev:
                max_rank_by_position_source[key] = rank_i

    existing = DynastyRankConsensusCurrent.query.all()
    existing_map = {(r.position, r.mfl_id): r for r in existing}

    touched_keys = set()
    writes = 0
    consensus_records_by_position: dict[str, list[DynastyRankConsensusCurrent]] = defaultdict(list)
    for key, items in grouped.items():
        position, mfl_id = key

        # rank by source for this player/position
        rank_by_source: dict[str, int] = {}
        for item in items:
            src = str(item.source).lower()
            if item.source_rank is not None:
                rank_by_source[src] = int(item.source_rank)

        effective_ranks: list[float] = []
        for src in sorted(sources_by_position.get(position, set())):
            if src in rank_by_source:
                effective_ranks.append(float(rank_by_source[src]))
                continue

            max_rank = max_rank_by_position_source.get((position, src), 0)
            # Missing-source penalty: use max_rank + 5 for that source/position.
            effective_ranks.append(float(max_rank + MISSING_SOURCE_RANK_PENALTY))

        consensus = sum(effective_ranks) / max(len(effective_ranks), 1)

        rec = existing_map.get((position, mfl_id))
        if rec is None:
            rec = DynastyRankConsensusCurrent(position=position, mfl_id=mfl_id)
            db.session.add(rec)

        rec.player_name = next((i.player_name for i in items if i.player_name), None)
        rec.consensus_rank = float(consensus)
        rec.sources_used = len(rank_by_source)  # count real contributing sources
        rec.updated_at_utc = now
        consensus_records_by_position[position].append(rec)
        writes += 1
        touched_keys.add((position, mfl_id))

    for position, records in consensus_records_by_position.items():
        sorted_records = sorted(
            records,
            key=lambda r: (
                float(r.consensus_rank) if r.consensus_rank is not None else float("inf"),
                (r.player_name or ""),
                str(r.mfl_id),
            ),
        )
        for idx, rec in enumerate(sorted_records, start=1):
            rec.positional_rank = idx

    for old_key, rec in existing_map.items():
        if old_key not in touched_keys:
            db.session.delete(rec)

    return writes


def _fetch_external_rows(source: str, position: str | None):
    if source == "fantasycalc":
        return adapt_fantasycalc_rows(fetch_and_rank_dynasty_values())

    if source == "keeptradecut":
        positions = [position] if position else ["QB", "RB", "WR", "TE"]
        all_rows = []
        for pos in positions:
            all_rows.extend(adapt_keeptradecut_rows(fetch_positional_rankings(pos)))
        return all_rows

    if source == "fantasypros":
        return adapt_fantasypros_rows(fetch_fantasypros_dynasty_trade_values(position=position))

    raise ValueError(f"Unsupported source: {source}")


def _run_one_source(source: str, *, position: str | None, recompute_consensus: bool) -> int:
    started = datetime.utcnow()
    audit = DynastyRankIngestAudit(source=source, started_at_utc=started, status="running")
    db.session.add(audit)
    db.session.flush()

    try:
        external_rows = _fetch_external_rows(source, position)
        if not external_rows:
            raise RuntimeError(f"{source} returned zero rows after parsing.")

        parsed_counts = defaultdict(int)
        for r in external_rows:
            parsed_counts[r.position] += 1

        matched, unmatched = map_rows_to_mfl(external_rows)
        mapped_counts = defaultdict(int)
        for r in matched:
            mapped_counts[r.position] += 1

        source_writes = _upsert_source_rows(matched)
        consensus_writes = _recompute_consensus_current() if recompute_consensus else 0

        audit.qb_count = parsed_counts.get("QB", 0)
        audit.rb_count = parsed_counts.get("RB", 0)
        audit.wr_count = parsed_counts.get("WR", 0)
        audit.te_count = parsed_counts.get("TE", 0)
        audit.status = "success"
        audit.finished_at_utc = datetime.utcnow()

        db.session.commit()

        print(f"[{source}] Source rows fetched: {len(external_rows)}")
        print(
            f"[{source}] Parsed counts: QB={audit.qb_count} RB={audit.rb_count} "
            f"WR={audit.wr_count} TE={audit.te_count}"
        )
        print(
            f"[{source}] Mapped counts: QB={mapped_counts.get('QB',0)} RB={mapped_counts.get('RB',0)} "
            f"WR={mapped_counts.get('WR',0)} TE={mapped_counts.get('TE',0)}"
        )
        print(f"[{source}] Unmatched rows: {len(unmatched)}")
        if unmatched:
            for row in unmatched[:10]:
                print(f"  - {row.position}: {row.name_raw} reason={row.reason}")
        print(f"[{source}] Source upserts: {source_writes}")
        print(f"[{source}] Consensus rows written: {consensus_writes}")
        print(f"[{source}] Audit id={audit.id} status={audit.status}")
        return 0
    except Exception as exc:
        db.session.rollback()
        audit.status = "failed"
        audit.error_summary = str(exc)
        audit.finished_at_utc = datetime.utcnow()
        db.session.add(audit)
        db.session.commit()
        print(f"[{source}] Refresh failed: {exc}")
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh dynasty ranks (source + consensus + audit).")
    parser.add_argument("--source", default="fantasycalc", choices=["fantasycalc", "keeptradecut", "fantasypros", "all"])
    parser.add_argument("--position", default=None, choices=["QB", "RB", "WR", "TE"], help="Optional for keeptradecut/fantasypros")
    parser.add_argument("--skip-consensus", action="store_true", help="Write source rows only")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        if args.source == "all":
            code1 = _run_one_source("fantasycalc", position=None, recompute_consensus=not args.skip_consensus)
            code2 = _run_one_source("keeptradecut", position=args.position, recompute_consensus=not args.skip_consensus)
            code3 = _run_one_source("fantasypros", position=args.position, recompute_consensus=not args.skip_consensus)
            raise SystemExit(0 if (code1 == 0 and code2 == 0 and code3 == 0) else 1)
        raise SystemExit(_run_one_source(args.source, position=args.position, recompute_consensus=not args.skip_consensus))


if __name__ == "__main__":
    main()