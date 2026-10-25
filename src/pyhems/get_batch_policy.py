"""GET batch policies for device-specific ECHONET Lite behavior."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GetBatchPolicy:
    """Constraints that apply to EPCs in one GET request."""

    incompatible_pairs: tuple[frozenset[int], ...] = ()
    max_batch_size: int | None = None


GET_BATCH_POLICIES: dict[tuple[int, int], GetBatchPolicy] = {
    (0x00000B, 0x0287): GetBatchPolicy(
        incompatible_pairs=(frozenset({0xB3, 0xB7}),),
    ),
    (0x0000E8, 0x0290): GetBatchPolicy(max_batch_size=14),
    (0x0000E8, 0x02A3): GetBatchPolicy(max_batch_size=9),
}

_POLICY_CLASSES = frozenset(class_code for _, class_code in GET_BATCH_POLICIES)


def has_get_batch_policy_for_class(class_code: int) -> bool:
    """Return whether any manufacturer-specific policy applies to a class."""
    return class_code in _POLICY_CLASSES


def get_get_batch_policy(
    manufacturer_code: int | None, class_code: int
) -> GetBatchPolicy | None:
    """Return the policy for a manufacturer and object class, if any."""
    if manufacturer_code is None:
        return None
    return GET_BATCH_POLICIES.get((manufacturer_code, class_code))


def _can_add_to_batch(
    batch: Iterable[int],
    epc: int,
    policy: GetBatchPolicy | None,
) -> bool:
    if policy is None:
        return True

    batch_epcs = set(batch)
    return all(
        not (epc in pair and bool((pair - {epc}) & batch_epcs))
        for pair in policy.incompatible_pairs
    )


def take_first_batch(
    epcs: Iterable[int],
    *,
    manufacturer_code: int | None,
    class_code: int,
    observed_batch_capacity: int | None = None,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Take the first batch and return the EPCs that remain.

    The remaining EPCs are intentionally not required to preserve the
    original request order. Each subsequent batch is planned lazily from the
    returned remainder.
    """
    if observed_batch_capacity is not None and observed_batch_capacity < 1:
        raise ValueError("observed_batch_capacity must be positive")

    policy = get_get_batch_policy(manufacturer_code, class_code)
    capacity = observed_batch_capacity
    if policy is not None and policy.max_batch_size is not None:
        capacity = (
            policy.max_batch_size
            if capacity is None
            else min(capacity, policy.max_batch_size)
        )
    batch: list[int] = []
    remaining: list[int] = []
    for epc in epcs:
        full = capacity is not None and len(batch) >= capacity
        if full or not _can_add_to_batch(batch, epc, policy):
            remaining.append(epc)
        else:
            batch.append(epc)

    return tuple(batch), tuple(remaining)


__all__ = [
    "GET_BATCH_POLICIES",
    "GetBatchPolicy",
    "get_get_batch_policy",
    "has_get_batch_policy_for_class",
    "take_first_batch",
]
