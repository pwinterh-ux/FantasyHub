"""Shared semantics for MFL lineup position constraints."""
from __future__ import annotations

from collections.abc import Mapping


def constraint_members(rule_key: str) -> frozenset[str]:
    """Return the actual positions counted by an MFL rule key.

    MFL expresses flex groups by joining positions with ``+``.  Empty pieces
    are ignored so malformed rules never accidentally match a player.
    """
    return frozenset(part.strip().upper() for part in str(rule_key).split("+")
                     if part.strip())


def count_for_constraint(actual_position_counts: Mapping[str, int], rule_key: str) -> int:
    """Aggregate actual-position counts for one simple or composite rule."""
    return sum(int(actual_position_counts.get(position, 0))
               for position in constraint_members(rule_key))


def allowed_actual_positions(ranges: Mapping) -> frozenset[str]:
    """Return every actual position accepted by parsed lineup constraints."""
    return frozenset(position for rule_key in ranges
                     for position in constraint_members(rule_key))


def lineup_satisfies_constraints(actual_position_counts: Mapping[str, int], ranges: Mapping,
                                  *, minimums: bool = True, maximums: bool = True) -> bool:
    """Test every lineup rule using its aggregated member-position count."""
    for rule_key, (minimum, maximum) in ranges.items():
        count = count_for_constraint(actual_position_counts, rule_key)
        if minimums and count < minimum:
            return False
        if maximums and count > maximum:
            return False
    return True
