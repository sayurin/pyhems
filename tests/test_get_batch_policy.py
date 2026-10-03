from __future__ import annotations

import pytest

from pyhems.get_batch_policy import plan_get_batches, take_first_batch


def test_panasonic_metering_policy_uses_first_fit() -> None:
    epcs = [0xC0, 0xC1, 0xC2, 0xB3, 0xB7, 0xB8, 0xBA]

    assert plan_get_batches(
        epcs,
        manufacturer_code=0x00000B,
        class_code=0x0287,
        observed_batch_capacity=6,
    ) == [
        (0xC0, 0xC1, 0xC2, 0xB3, 0xB8, 0xBA),
        (0xB7,),
    ]


@pytest.mark.parametrize(
    ("manufacturer_code", "class_code"),
    [
        (0x000001, 0x0287),
        (0x00000B, 0x0130),
    ],
)
def test_unmatched_device_does_not_apply_conflict_policy(
    manufacturer_code: int, class_code: int
) -> None:
    epcs = [0xC0, 0xB3, 0xB7, 0xBA]

    assert plan_get_batches(
        epcs,
        manufacturer_code=manufacturer_code,
        class_code=class_code,
    ) == [tuple(epcs)]


def test_capacity_and_conflict_constraints_are_applied_together() -> None:
    epcs = [0xC0, 0xC1, 0xC2, 0xB3, 0xB7, 0xB8, 0xBA]

    assert plan_get_batches(
        epcs,
        manufacturer_code=0x00000B,
        class_code=0x0287,
        observed_batch_capacity=3,
    ) == [
        (0xC0, 0xC1, 0xC2),
        (0xB3, 0xB8, 0xBA),
        (0xB7,),
    ]


def test_nonpositive_capacity_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        plan_get_batches(
            [0x80],
            manufacturer_code=None,
            class_code=0x0130,
            observed_batch_capacity=0,
        )


def test_take_first_batch_returns_lazy_remainder() -> None:
    first, remaining = take_first_batch(
        [0xC0, 0xC1, 0xC2, 0xB3, 0xB7, 0xB8, 0xBA],
        manufacturer_code=0x00000B,
        class_code=0x0287,
        observed_batch_capacity=6,
    )

    assert first == (0xC0, 0xC1, 0xC2, 0xB3, 0xB8, 0xBA)
    assert remaining == (0xB7,)
