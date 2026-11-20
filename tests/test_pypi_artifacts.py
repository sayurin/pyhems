"""Tests for safe retries of PyPI publication."""

import hashlib
import io
import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

import pytest
import yaml

from scripts import check_pypi_artifacts

ROOT = Path(__file__).parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())


@pytest.fixture
def distributions(tmp_path: Path) -> Path:
    (tmp_path / "pyhems-1.2.3.tar.gz").write_bytes(b"source")
    (tmp_path / "pyhems-1.2.3-py3-none-any.whl").write_bytes(b"wheel")
    return tmp_path


def response_for(directory: Path, names: list[str] | None = None) -> io.BytesIO:
    selected = names or [path.name for path in directory.iterdir()]
    urls = [
        {
            "filename": name,
            "digests": {
                "sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest()
            },
        }
        for name in selected
    ]
    return io.BytesIO(json.dumps({"urls": urls}).encode())


def mocked_response(response: io.BytesIO) -> Callable[..., io.BytesIO]:
    content = response.getvalue()

    def open_url(request: Request, *, timeout: int) -> io.BytesIO:
        assert request.full_url == "https://pypi.org/pypi/pyhems/1.2.3/json"
        assert timeout == 30
        return io.BytesIO(content)

    return open_url


def test_unpublished_version_is_allowed_before_publication(
    distributions: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing_version(request: Request, *, timeout: int):
        assert request.full_url == "https://pypi.org/pypi/pyhems/1.2.3/json"
        assert timeout == 30
        raise HTTPError("https://pypi.org", 404, "Not Found", None, None)

    monkeypatch.setattr(check_pypi_artifacts, "urlopen", missing_version)
    check_pypi_artifacts.verify_artifacts("v1.2.3", distributions)
    with pytest.raises(ValueError, match="no distributions"):
        check_pypi_artifacts.verify_artifacts("v1.2.3", distributions, complete=True)


def test_matching_partial_publication_is_retryable(
    distributions: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        check_pypi_artifacts,
        "urlopen",
        mocked_response(response_for(distributions, ["pyhems-1.2.3.tar.gz"])),
    )
    check_pypi_artifacts.verify_artifacts("v1.2.3", distributions)
    with pytest.raises(ValueError, match="missing"):
        check_pypi_artifacts.verify_artifacts("v1.2.3", distributions, complete=True)


@pytest.mark.parametrize(
    ("published", "error"),
    [
        ({"filename": "pyhems-1.2.3.tar.gz", "digest": "bad"}, "mismatched"),
        ({"filename": "unexpected.whl", "digest": "bad"}, "unexpected"),
    ],
)
def test_mismatched_or_unexpected_pypi_files_fail(
    distributions: Path,
    monkeypatch: pytest.MonkeyPatch,
    published: dict[str, str],
    error: str,
) -> None:
    payload = {
        "urls": [
            {
                "filename": published["filename"],
                "digests": {"sha256": published["digest"]},
            }
        ]
    }
    monkeypatch.setattr(
        check_pypi_artifacts,
        "urlopen",
        mocked_response(io.BytesIO(json.dumps(payload).encode())),
    )
    with pytest.raises(ValueError, match=error):
        check_pypi_artifacts.verify_artifacts("v1.2.3", distributions)


def test_complete_matching_publication_passes(
    distributions: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        check_pypi_artifacts,
        "urlopen",
        mocked_response(response_for(distributions)),
    )
    check_pypi_artifacts.verify_artifacts("v1.2.3", distributions, complete=True)


def test_complete_publication_ignores_attestation_sidecars(
    distributions: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = response_for(distributions)
    for name in ("pyhems-1.2.3.tar.gz", "pyhems-1.2.3-py3-none-any.whl"):
        (distributions / f"{name}.publish.attestation").write_bytes(b"attestation")
    monkeypatch.setattr(check_pypi_artifacts, "urlopen", mocked_response(response))
    check_pypi_artifacts.verify_artifacts("v1.2.3", distributions, complete=True)


def test_orphan_attestation_is_rejected(distributions: Path) -> None:
    (distributions / "missing.whl.publish.attestation").write_bytes(b"attestation")
    with pytest.raises(ValueError, match="Attestation"):
        check_pypi_artifacts.local_digests(distributions)


def test_attestations_do_not_hide_mismatched_distributions(
    distributions: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = response_for(distributions)
    (distributions / "pyhems-1.2.3.tar.gz").write_bytes(b"changed")
    (distributions / "pyhems-1.2.3.tar.gz.publish.attestation").write_bytes(
        b"attestation"
    )
    monkeypatch.setattr(check_pypi_artifacts, "urlopen", mocked_response(response))
    with pytest.raises(ValueError, match="mismatched"):
        check_pypi_artifacts.verify_artifacts("v1.2.3", distributions, complete=True)


def test_attestations_without_distributions_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "pyhems-1.2.3.tar.gz.publish.attestation").write_bytes(b"attestation")
    with pytest.raises(ValueError, match="distribution"):
        check_pypi_artifacts.local_digests(tmp_path)


@pytest.mark.parametrize("initial", [None, {"pyhems-1.2.3.tar.gz": "source"}])
def test_complete_verification_waits_for_pypi(
    distributions: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial: dict[str, str] | None,
) -> None:
    expected = check_pypi_artifacts.local_digests(distributions)
    if initial is not None:
        initial = {"pyhems-1.2.3.tar.gz": expected["pyhems-1.2.3.tar.gz"]}
    responses = iter([initial, expected])
    monkeypatch.setattr(check_pypi_artifacts, "pypi_digests", lambda _: next(responses))
    waits: list[float] = []
    monkeypatch.setattr(check_pypi_artifacts.time, "sleep", waits.append)
    check_pypi_artifacts.verify_artifacts(
        "v1.2.3", distributions, complete=True, wait=True
    )
    assert len(waits) == 1


def test_complete_verification_stops_after_wait_limit(
    distributions: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(check_pypi_artifacts, "pypi_digests", lambda _: None)
    waits: list[float] = []
    monkeypatch.setattr(check_pypi_artifacts.time, "sleep", waits.append)
    with pytest.raises(ValueError, match="no distributions"):
        check_pypi_artifacts.verify_artifacts(
            "v1.2.3", distributions, complete=True, wait=True
        )
    assert sum(waits) == 60


@pytest.mark.parametrize(
    ("published", "error"),
    [
        ({"pyhems-1.2.3.tar.gz": "bad"}, "mismatched"),
        ({"unexpected.whl": "bad"}, "unexpected"),
    ],
)
def test_complete_verification_does_not_retry_invalid_files(
    distributions: Path,
    monkeypatch: pytest.MonkeyPatch,
    published: dict[str, str],
    error: str,
) -> None:
    monkeypatch.setattr(
        check_pypi_artifacts,
        "pypi_digests",
        lambda _: published,
    )
    waits: list[float] = []
    monkeypatch.setattr(check_pypi_artifacts.time, "sleep", waits.append)
    with pytest.raises(ValueError, match=error):
        check_pypi_artifacts.verify_artifacts(
            "v1.2.3", distributions, complete=True, wait=True
        )
    assert not waits


def test_release_workflow_publishes_directly_and_validates_recovery():
    config = json.loads((ROOT / "release-please-config.json").read_text())
    assert not config["packages"]["."]["draft"]
    assert not config["packages"]["."]["prerelease"]
    events = WORKFLOW[True]
    assert events["push"]["branches"] == ["master"]
    assert "release" not in events

    jobs = WORKFLOW["jobs"]
    assert jobs["prepare-release"]["outputs"]["sha"] == (
        "${{ steps.release.outputs.sha }}"
    )
    assert jobs["prepare-release"]["steps"][0]["with"]["ref"] == ("${{ github.sha }}")
    assert "refs/heads/master" in jobs["prepare-release"]["if"]
    verify = next(
        step
        for step in jobs["prepare-release"]["steps"]
        if step.get("name") == "Validate release state and resolve immutable revision"
    )
    assert "isDraft" in verify["run"]
    assert "isPrerelease" in verify["run"]
    assert "--clobber" not in json.dumps(WORKFLOW)
    publish = next(
        step
        for step in jobs["publish"]["steps"]
        if step.get("name") == "Publish to PyPI"
    )
    assert publish["with"]["skip-existing"]
    assert not any(
        "uv build" in step.get("run", "") for step in jobs["publish"]["steps"]
    )
    verify_published = next(
        step
        for step in jobs["publish"]["steps"]
        if step.get("name") == "Verify all published PyPI files"
    )
    assert "GITHUB_SHA" in verify_published["run"]
    assert "--wait" in verify_published["run"]


@pytest.fixture
def release_assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    assets = tmp_path / "assets"
    assets.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        """#!/bin/bash
set -euo pipefail
if [[ "$2" == view ]]; then
  for file in "$MOCK_ASSETS"/*; do
    [[ -e "$file" ]] && basename "$file"
  done
  exit 0
fi
pattern="$5"
directory="$7"
cp "$MOCK_ASSETS/$pattern" "$directory/$pattern"
"""
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("MOCK_ASSETS", str(assets))
    monkeypatch.setenv("TAG_NAME", "v1.2.3")
    return assets


@pytest.mark.parametrize(
    "existing", ["none", "complete", "wheel", "sdist", "unexpected"]
)
def test_release_distribution_selection(
    tmp_path: Path, release_assets: Path, existing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if existing in {"wheel", "complete"}:
        (release_assets / "pyhems-1.2.3-py3-none-any.whl").write_bytes(b"wheel")
    if existing in {"sdist", "complete"}:
        (release_assets / "pyhems-1.2.3.tar.gz").write_bytes(b"source")
    if existing == "unexpected":
        (release_assets / "unexpected.whl").write_bytes(b"unexpected")
    output = tmp_path / "output"
    output.touch()
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    step = next(
        step
        for step in WORKFLOW["jobs"]["prepare-release"]["steps"]
        if step.get("name") == "Select release distributions"
    )
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if existing not in {"none", "complete"}:
        assert result.returncode != 0
        assert (
            "Unexpected" if existing == "unexpected" else "incomplete"
        ) in result.stdout
        assert not (tmp_path / "dist").exists()
    else:
        assert result.returncode == 0, result.stderr
        assert (
            f"build={'true' if existing == 'none' else 'false'}" in output.read_text()
        )
        if existing == "complete":
            assert (tmp_path / "dist/pyhems-1.2.3.tar.gz").read_bytes() == b"source"
            assert (
                tmp_path / "dist/pyhems-1.2.3-py3-none-any.whl"
            ).read_bytes() == b"wheel"


@pytest.mark.parametrize("changed", [False, True])
def test_publish_download_checks_prepared_hashes(
    tmp_path: Path,
    release_assets: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed: bool,
) -> None:
    (release_assets / "pyhems-1.2.3-py3-none-any.whl").write_bytes(
        b"changed" if changed else b"wheel"
    )
    (release_assets / "pyhems-1.2.3.tar.gz").write_bytes(b"source")
    monkeypatch.setenv("WHEEL_SHA256", hashlib.sha256(b"wheel").hexdigest())
    monkeypatch.setenv("SDIST_SHA256", hashlib.sha256(b"source").hexdigest())
    step = next(
        step
        for step in WORKFLOW["jobs"]["publish"]["steps"]
        if step.get("name") == "Download and verify release distributions"
    )
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode != 0) == changed
    if not changed:
        assert (
            tmp_path / "dist/pyhems-1.2.3-py3-none-any.whl"
        ).read_bytes() == b"wheel"
