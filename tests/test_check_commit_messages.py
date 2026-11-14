"""Tests for Conventional Commit subject validation."""

import sys

import pytest

from scripts.check_commit_messages import is_conventional_subject, main


def test_accepts_conventional_commit_subjects() -> None:
    assert is_conventional_subject("fix: handle timeout")
    assert is_conventional_subject("feat(runtime): add retries")
    assert is_conventional_subject("feat!: change the public API")


def test_rejects_non_conventional_subjects() -> None:
    assert not is_conventional_subject("Handle timeout")
    assert not is_conventional_subject("Fix: handle timeout")
    assert not is_conventional_subject("fix: ")


@pytest.mark.parametrize(
    ("title", "expected"),
    [("fix: handle timeout", 0), ("Handle timeout", 1), ("", 1)],
)
def test_checks_pull_request_title(
    monkeypatch: pytest.MonkeyPatch, title: str, expected: int
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["check_commit_messages.py", "HEAD", "HEAD", "--title", title]
    )
    assert main() == expected
