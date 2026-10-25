from __future__ import annotations

import pytest

from pyhems.get_batch_policy import take_first_batch


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

    assert take_first_batch(
        epcs,
        manufacturer_code=manufacturer_code,
        class_code=class_code,
    ) == (tuple(epcs), ())


def test_capacity_and_conflict_constraints_are_applied_together() -> None:
    epcs = [0xC0, 0xC1, 0xC2, 0xB3, 0xB7, 0xB8, 0xBA]

    assert take_first_batch(
        epcs,
        manufacturer_code=0x00000B,
        class_code=0x0287,
        observed_batch_capacity=3,
    ) == ((0xC0, 0xC1, 0xC2), (0xB3, 0xB7, 0xB8, 0xBA))


def test_nonpositive_capacity_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        take_first_batch(
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


@pytest.mark.parametrize(
    ("class_code", "max_batch_size"),
    [
        (0x0290, 14),
        (0x02A3, 9),
    ],
)
def test_koizumi_policy_caps_batch_size(class_code: int, max_batch_size: int) -> None:
    epcs = list(range(0x80, 0x80 + 27))

    first, remaining = take_first_batch(
        epcs,
        manufacturer_code=0x0000E8,
        class_code=class_code,
    )

    assert first == tuple(epcs[:max_batch_size])
    assert remaining == tuple(epcs[max_batch_size:])


def test_smaller_observed_capacity_wins_over_policy_cap() -> None:
    epcs = list(range(0x80, 0x80 + 27))

    assert take_first_batch(
        epcs,
        manufacturer_code=0x0000E8,
        class_code=0x0290,
        observed_batch_capacity=5,
    ) == (tuple(epcs[:5]), tuple(epcs[5:]))


def test_policy_cap_wins_over_larger_observed_capacity() -> None:
    epcs = list(range(0x80, 0x80 + 27))

    assert take_first_batch(
        epcs,
        manufacturer_code=0x0000E8,
        class_code=0x0290,
        observed_batch_capacity=20,
    ) == (tuple(epcs[:14]), tuple(epcs[14:]))


def test_policy_cap_does_not_apply_to_other_manufacturers() -> None:
    epcs = list(range(0x80, 0x80 + 27))

    assert take_first_batch(
        epcs,
        manufacturer_code=0x00000B,
        class_code=0x0290,
    ) == (tuple(epcs), ())
