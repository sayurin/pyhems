"""Tests for safe retries of PyPI publication."""

import hashlib
import io
import json
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
