"""Verify that already-published PyPI files match local distributions."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def local_digests(directory: Path) -> dict[str, str]:
    """Return SHA-256 digests for every distribution in a build directory."""
    artifacts = sorted(path for path in directory.iterdir() if path.is_file())
    if not artifacts:
        raise ValueError(f"No distribution files found in {directory}")
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in artifacts
    }


def pypi_digests(version: str) -> dict[str, str] | None:
    """Fetch PyPI's published filenames and SHA-256 digests for a version."""
    version = version.removeprefix("v")
    request = Request(
        f"https://pypi.org/pypi/pyhems/{version}/json",
        headers={"User-Agent": "pyhems-release-verification"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            release = json.load(response)
    except HTTPError as error:
        if error.code == 404:
            return None
        raise
    return {
        artifact["filename"]: artifact["digests"]["sha256"]
        for artifact in release["urls"]
    }


def verify_artifacts(version: str, directory: Path, *, complete: bool = False) -> None:
    """Reject mismatched existing files and optionally require a complete upload."""
    expected = local_digests(directory)
    published = pypi_digests(version)
    if published is None:
        if complete:
            raise ValueError(f"PyPI has no distributions for version {version}")
        return

    unexpected = published.keys() - expected.keys()
    mismatched = {
        name
        for name, digest in published.items()
        if name in expected and digest != expected[name]
    }
    missing = expected.keys() - published.keys()
    if unexpected or mismatched or (complete and missing):
        raise ValueError(
            "PyPI distribution mismatch: "
            f"unexpected={sorted(unexpected)}, "
            f"mismatched={sorted(mismatched)}, "
            f"missing={sorted(missing) if complete else []}"
        )


def main() -> None:
    """Verify local distributions against the public PyPI release."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--complete", action="store_true")
    arguments = parser.parse_args()
    verify_artifacts(
        arguments.version, arguments.directory, complete=arguments.complete
    )


if __name__ == "__main__":
    main()
