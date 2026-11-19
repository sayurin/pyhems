"""Tests for Conventional Commit subject validation."""

import sys

import pytest

from scripts.check_commit_messages import is_conventional_subject, main


@pytest.mark.parametrize(
    "commit_type",
    [
        "feat",
        "fix",
        "perf",
        "deps",
        "revert",
        "docs",
        "style",
        "chore",
        "refactor",
        "test",
        "build",
        "ci",
    ],
)
def test_accepts_release_please_python_types(commit_type: str) -> None:
    assert is_conventional_subject(f"{commit_type}(scope): update component")


def test_accepts_breaking_change_subjects() -> None:
    assert is_conventional_subject("feat!: change the public API")


def test_rejects_non_conventional_subjects() -> None:
    assert not is_conventional_subject("Handle timeout")
    assert not is_conventional_subject("Fix: handle timeout")
    assert not is_conventional_subject("fix: ")
    assert not is_conventional_subject("device-manager: remove readback refresh")


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("fix(device-manager): remove readback refresh", 0),
        ("chore(master): release 0.9.2", 0),
        ("device-manager: remove readback refresh", 1),
        ("Handle timeout", 1),
        ("", 1),
    ],
)
def test_checks_pull_request_title(
    monkeypatch: pytest.MonkeyPatch, title: str, expected: int
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["check_commit_messages.py", "HEAD", "HEAD", "--title", title]
    )
    assert main() == expected
