"""Verify that already-published PyPI files match local distributions."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def local_digests(directory: Path) -> dict[str, str]:
    """Return SHA-256 digests for every distribution in a build directory."""
    files = sorted(path for path in directory.iterdir() if path.is_file())
    suffix = ".publish.attestation"
    artifacts = [path for path in files if not path.name.endswith(suffix)]
    if not artifacts:
        raise ValueError(f"No distribution files found in {directory}")
    names = {path.name for path in artifacts}
    orphaned = [
        path.name
        for path in files
        if path.name.endswith(suffix) and path.name.removesuffix(suffix) not in names
    ]
    if orphaned:
        raise ValueError(f"Attestation without distribution: {orphaned}")
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


def verify_artifacts(
    version: str, directory: Path, *, complete: bool = False, wait: bool = False
) -> None:
    """Reject mismatched existing files and optionally require a complete upload."""
    if wait and not complete:
        raise ValueError("Waiting requires complete verification")
    expected = local_digests(directory)
    attempts = 13 if wait else 1
    for attempt in range(attempts):
        published = pypi_digests(version)
        if published is None:
            if not complete:
                return
            error = f"PyPI has no distributions for version {version}"
        else:
            unexpected = published.keys() - expected.keys()
            mismatched = {
                name
                for name, digest in published.items()
                if name in expected and digest != expected[name]
            }
            missing = expected.keys() - published.keys()
            error = (
                "PyPI distribution mismatch: "
                f"unexpected={sorted(unexpected)}, "
                f"mismatched={sorted(mismatched)}, "
                f"missing={sorted(missing) if complete else []}"
            )
            if unexpected or mismatched:
                raise ValueError(error)
            if not complete or not missing:
                return
        if attempt + 1 == attempts:
            raise ValueError(error)
        sys.stderr.write(f"{error}; retrying in 5 seconds\n")
        time.sleep(5)


def main() -> None:
    """Verify local distributions against the public PyPI release."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--complete", action="store_true")
    parser.add_argument("--wait", action="store_true")
    arguments = parser.parse_args()
    verify_artifacts(
        arguments.version,
        arguments.directory,
        complete=arguments.complete,
        wait=arguments.wait,
    )


if __name__ == "__main__":
    main()
