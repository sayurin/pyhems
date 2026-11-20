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

Use English Conventional Commit subjects. The type must be one of the
Release Please Python strategy's default changelog sections:
`feat`, `fix`, `perf`, `deps`, `revert`, `docs`, `style`, `chore`,
`refactor`, `test`, `build`, or `ci`. Other types are rejected, even though
Conventional Commits permits custom types. Use a scope for the affected
component, for example `fix(device-manager): ...`.

Examples:

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
Review and merge that pull request to create a published GitHub Release and
tag. The Release workflow builds the distributions from that tag, attaches
them to the release, verifies the same files against PyPI, and publishes them
through Trusted Publishing.

Repository setup required for automation:

- Install a GitHub App on this repository with Contents, Pull requests, and
  Issues read and write permissions. Set its Client ID as the `APP_CLIENT_ID`
  Actions variable and its private key as the `APP_PRIVATE_KEY` Actions secret.
  The workflow creates a short-lived App token so CI runs for Release Please
  pull requests.
- Configure the `pypi` Actions environment as a PyPI Trusted Publisher for
  `.github/workflows/release.yml`.
- Require the CI checks before merging pull requests.
- Allow squash merging only, and set the default squash commit title to the
  pull request title. Do not edit the title when confirming the merge.
- Install the pinned GitHub CLI extension with
  `gh extension install github/gh-actions-lock --pin v0.4.1`; prek checks local
  lockfile coverage, and CI verifies the resolved pins against GitHub.

If release preparation or PyPI publication fails, rerun the `Release` workflow
on `master` with the existing published tag. Recovery verifies the tag,
version, release state, GitHub assets, and any existing PyPI files. It only
adds missing files; mismatched files fail explicitly and are never overwritten.
