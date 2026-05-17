# Release Runbook

How to cut an OpsHub release. `v0.1.0` is the first public release; subsequent
patch / minor releases follow the same flow.

**Distribution channel for v0.1.x**: Git source via `uv tool install`. PyPI
publishing is deferred (see [§Future: PyPI migration](#future-pypi-migration)
below and [ADR-0001 §Updates](adr/0001-python-stack.md#updates)).

The release workflow (`.github/workflows/release.yaml`) builds + verifies
the artifact on every tag push but does **not** publish to PyPI in the v0.1.x
line. Operators install directly from this repo:

```bash
uv tool install git+https://github.com/ozzy-labs/opshub.git@v0.1.0
```

The PyPI publish job is preserved in the workflow (commented out, ready to
re-enable). See §Future for the re-enable steps.

## One-time setup

The git-source distribution path requires **zero one-time setup** — no PyPI
account, no Trusted Publisher registration, no `pypi` GitHub environment.
The repository is public so anonymous `uv tool install git+https://...`
just works.

Optional but recommended:

- **Enable [GitHub Releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)
  protection rules**: under repo Settings → Code and automation → Releases,
  restrict who can publish releases. Helpful as the project grows; not
  blocking for v0.1.0.

## Per-release flow

### 1. Confirm prerequisites

- [ ] All target work merged to `main`
- [ ] `just ci` green on `main`
- [ ] `CHANGELOG.md` has an unreleased entry promoted to the new version
- [ ] `pyproject.toml` `version` matches the next intended tag (without the
      leading `v`)
- [ ] `src/opshub/__init__.py` `__version__` matches `pyproject.toml`
- [ ] `docs/release-notes-v<X.Y.Z>.md` exists and reads sensibly (for minor /
      major releases; optional for patch fixes)

The `build` job in `release.yaml` verifies tag vs `__version__` and fails
fast on mismatch. Catching it locally is still faster (delete + re-push tag
is annoying).

### 2. Tag + push

```bash
git switch main
git pull --ff-only origin main

# Pick the next version per SemVer.
# 0.x stance: minor bumps for any change, patch for bug-fix-only.
new_version="0.1.0"

git tag -a "v${new_version}" -m "Release v${new_version}"
git push origin "v${new_version}"
```

The tag must match the regex `v*.*.*` exactly. `v0.1.0` ✅, `0.1.0` ❌,
`v0.1` ❌, `v0.1.0-rc1` ❌ (pre-release tagging is not yet wired in).

### 3. Observe the release workflow

Pushing the tag fires `.github/workflows/release.yaml`. One job runs in the
v0.1.x line:

1. **`build`** — verifies `__version__` matches the tag, runs `uv build` to
   produce sdist + wheel under `dist/`, uploads as a workflow artifact (90-day
   retention).

Watch progress:

- <https://github.com/ozzy-labs/opshub/actions/workflows/release.yaml>

The artifact is downloadable from the workflow run for ~90 days. Operators
who prefer pre-built wheels over `uv tool install git+...` can grab it from
there, but the documented primary path is git source.

### 4. Verify the install path

After the tag is pushed (no need to wait for the workflow if you trust local
`just ci`):

```bash
# Clean install from a freshly tagged ref
uv tool uninstall opshub 2>/dev/null
uv tool install "git+https://github.com/ozzy-labs/opshub.git@v${new_version}"
opshub --version
# expected: opshub <new_version>

# Smoke test:
opshub init
opshub task create "Smoke test from git source install"
opshub task list
```

If `opshub --version` reports a stale version, your local `uv tool` cache is
in the way — the `uv tool uninstall` above clears it.

### 5. GitHub Release

Create the GitHub Release backed by the tag, using the release-notes file as
the body:

```bash
gh release create "v${new_version}" \
  --title "v${new_version}" \
  --notes-file "docs/release-notes-v${new_version}.md"
```

For patch releases that don't justify a full narrative, generate notes from
the CHANGELOG diff instead:

```bash
gh release create "v${new_version}" \
  --title "v${new_version}" \
  --notes "$(awk "/^## \\[${new_version}\\]/,/^## \\[/" CHANGELOG.md | sed '$d')"
```

Or do either via the Web UI: <https://github.com/ozzy-labs/opshub/releases/new>

The GitHub Release acts as the discoverable surface for v0.1.x — it surfaces
the release notes, the tag, and (optionally) attached pre-built wheels. The
Release page also lets external link tools (e.g. shields.io's
`github/v/release` badge in the README) pick up the latest version
automatically.

The release-notes files live in `docs/release-notes-v<X.Y.Z>.md` so they stay
discoverable on the default branch even after the GitHub Release UI is
restructured.

### 6. (Optional) Attach pre-built wheel to the Release

If users want pre-built wheel installs (faster than `git+...` which builds
from source on each install), download the artifact from the workflow run
and attach it to the Release:

```bash
gh run list --workflow=release.yaml --limit 1 --json databaseId --jq '.[0].databaseId' \
  | xargs -I{} gh run download {} --dir /tmp/opshub-release
gh release upload "v${new_version}" /tmp/opshub-release/dist/*
```

Then users can:

```bash
uv tool install https://github.com/ozzy-labs/opshub/releases/download/v0.1.0/opshub-0.1.0-py3-none-any.whl
```

This is a convenience, not required.

### 7. Announce

For minor / major releases:

- [ ] Update any project social media / blog (if applicable)
- [ ] Notify users in any relevant communities

For patch releases:

- [ ] No announcement needed unless the patch closes a CVE or fixes a
      data-loss bug.

## Troubleshooting

### Tag pushed but workflow didn't fire

Tags must match `v*.*.*` (SemVer). `v0.1.0` ✅, `0.1.0` (no `v`) ❌, `v0.1`
❌. Confirm via:

```bash
gh run list --workflow=release.yaml --limit 5
```

If the run isn't listed, the tag didn't match the filter. Push a new tag with
the correct shape; the bad tag can be deleted from GitHub afterwards.

### Version mismatch

The `build` job's verify step rejects tags that don't match `__version__`:

```text
::error::tag v0.1.0 does not match package version 0.0.1
```

Fix `pyproject.toml` + `src/opshub/__init__.py`, commit, then:

```bash
git tag -d "v${new_version}"
git push origin ":refs/tags/v${new_version}"
git tag -a "v${new_version}" -m "Release v${new_version}"
git push origin "v${new_version}"
```

### Need to "yank" a release

Git source distribution has no central yank mechanism (`uv tool install
git+...@v0.1.0` will always succeed as long as the tag exists). Two
options:

1. **Mark the GitHub Release as "Pre-release" or "Draft"** to signal "do not
   install this version" to users browsing releases. This does NOT block
   direct tag installs.
2. **Ship a patched release (`X.Y.Z+1`) ASAP** and update the README +
   release notes to advise upgrading. The migration to PyPI (where real
   yank semantics exist) is in the §Future plan.

For data-loss bugs, also delete the bad tag if no one has cloned it yet:

```bash
git push origin ":refs/tags/v${bad_version}"
# Then via Web UI: delete the corresponding GitHub Release
```

Deleting a tag that has been pulled by users does NOT remove their local
installs — they must `uv tool upgrade` to the patched version themselves.

## Future: PyPI migration

When OpsHub gains broader adoption and the `uv tool install git+...` syntax
becomes a friction point, migrate to PyPI. The release workflow already
contains a commented-out PyPI publish job; the migration is:

1. **PyPI account + Trusted Publisher registration** (Web UI, ~5 min):
   - Create or sign in at <https://pypi.org/account/register/>
   - Enable 2FA (PyPI enforces this)
   - Go to <https://pypi.org/manage/account/publishing/> → **Add a new
     pending publisher**
   - Owner: `ozzy-labs` / Repository: `opshub` / Workflow: `release.yaml`
     / Environment: `pypi` / Project name: `opshub`
2. **Configure the `pypi` GitHub environment** (Web UI):
   - <https://github.com/ozzy-labs/opshub/settings/environments> → **New
     environment** → name = `pypi`
   - (Recommended) Add required reviewers
   - (Optional) Restrict to selected tags `v*.*.*`
3. **Uncomment the publish job** in `.github/workflows/release.yaml` (it's
   intentionally left in place with `# TODO(pypi):` markers).
4. **Update README install command** from `uv tool install git+...` to
   `uv tool install opshub` (and the PyPI badge from
   `img.shields.io/github/v/release/...` to `img.shields.io/pypi/v/opshub`).
5. **Update this runbook**: replace §"Distribution channel" header note +
   add the §"Verify on PyPI" sub-step to the per-release flow.
6. **Ship the first PyPI release** following the (now-enabled) publish job —
   tag a `v0.x.y` and let the workflow handle the publish via OIDC. No
   static `PYPI_TOKEN`; Trusted Publishers handle auth.

Until step 6 lands, `uv tool install opshub` (bare, no URL) will 404 — so
README, RUNBOOK, and release notes should not advertise that path.

## References

- [`.github/workflows/release.yaml`](../.github/workflows/release.yaml) — the
  workflow this runbook describes
- [`CHANGELOG.md`](../CHANGELOG.md) — release history
- [`docs/upgrading.md`](upgrading.md) — end-user version migration notes
- [ADR-0001 §Updates](adr/0001-python-stack.md#updates) — distribution choice
  rationale (Phase 8.x sqlite-vec promotion + git-source distribution)
- [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) / [SemVer](https://semver.org/spec/v2.0.0.html)
- [PyPI Trusted Publishers docs](https://docs.pypi.org/trusted-publishers/) — for the future PyPI migration
