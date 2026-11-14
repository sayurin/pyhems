# Contributing

## Development

Use the Python version in `.python-version` and install the locked development
environment with uv:

```shell
uv sync --locked --extra dev
uv run --locked --extra dev pytest
uv run --locked --extra dev prek run --all-files
uv audit --locked
uv build
```

The `dev` extra is the source of truth for Python development tools.
Update `uv.lock` whenever `pyproject.toml` changes; CI uses `--locked` to reject
an out-of-date lockfile.

## Commits and pull requests

Use English Conventional Commit subjects, for example:

```text
fix: handle incomplete responses
feat(runtime): add request timeout configuration
```

Pull request titles and non-merge commit subjects are checked in CI. The title
must follow the same format, including for release pull requests. Squash merge
pull requests and use the pull request title as the resulting commit subject.
CI also rechecks the title when a pull request is edited.

## Releases

Release Please proposes version and changelog updates in a pull request.
Review and merge that pull request to create a draft GitHub Release. Review the
draft and publish it manually; the published tag is then used to build the
distributions for PyPI and the GitHub Release.

Repository setup required for automation:

- Add a `RELEASE_PLEASE_TOKEN` Actions secret with permission to create and
  update pull requests and releases. Using this token allows CI to run for
  Release Please pull requests.
- Configure the `pypi` Actions environment as a PyPI Trusted Publisher for
  `.github/workflows/release.yml`.
- Require the CI checks before merging pull requests.
- Allow squash merging only, and set the default squash commit title to the
  pull request title. Do not edit the title when confirming the merge.
- Install the pinned GitHub CLI extension with
  `gh extension install github/gh-actions-lock --pin v0.4.1`; prek checks local
  lockfile coverage, and CI verifies the resolved pins against GitHub.

If preparing a draft release fails, rerun the `Release` workflow with the
existing tag to rebuild and reattach its distributions. If PyPI publication
fails after a release is published, rerun the failed job; existing files are
skipped rather than overwritten.
