# Release Runbook

How to cut an OpsHub release. `v0.1.0` is the first public release; subsequent
patch / minor releases follow the same flow.

**Distribution**: PyPI under the name **`ozzylabs-opshub`** (PEP 423
`<owner>-<package>` form, because PyPI has no namespace concept and the bare
`opshub` name was unavailable). The CLI command stays `opshub`. See
[ADR-0001 §Updates](adr/0001-python-stack.md#updates) for the naming
rationale.

The release workflow (`.github/workflows/release.yaml`) builds, verifies tag
vs `__version__`, and publishes to PyPI via OIDC. Everything that requires a
human decision (version bump, CHANGELOG, release notes) stays here, in this
runbook, rather than buried in YAML.

## One-time setup (before first release)

The two steps below can only be performed via the GitHub Web UI / PyPI Web UI
— there is no API surface that lets `gh` / `uv` / a workflow script perform
them on the operator's behalf. Do them once, then forget about them.

### 1. PyPI account + Trusted Publisher registration

Trusted Publishers (OIDC) replace static `PYPI_TOKEN` secrets. The release
workflow authenticates to PyPI via short-lived OIDC tokens scoped to this
GitHub repo + the `pypi` environment, matching the project's [Trusted
Publishers stance](../CLAUDE.md) (static long-lived registry tokens are
forbidden for new releases).

Steps (one time, requires repo admin + PyPI account):

1. **Create / sign in to a PyPI account** at
   <https://pypi.org/account/register/>. The `ozzylabs` account owns the
   `ozzylabs-opshub` distribution. Enable 2FA (PyPI enforces this for
   publishers).

2. **Reserve the project name via Pending Publisher.** For a brand-new
   project, no upload is required upfront:

   - Go to <https://pypi.org/manage/account/publishing/>
   - Click **Add a new pending publisher**
   - Fill in:
     - **PyPI Project Name:** `ozzylabs-opshub`
     - **Owner:** `ozzy-labs` (the GitHub org, not the PyPI account name)
     - **Repository name:** `opshub`
     - **Workflow name:** `release.yaml`
     - **Environment name:** `pypi`
   - Submit. The pending publisher converts into a real publisher on the
     first successful upload.

   If the form rejects a name as "too similar to an existing project" or
   "name unavailable", try a different prefix (`opshub-ai`, `ozzylabs-opshub-cli`,
   etc.) and update `pyproject.toml [project] name` to match before tagging.

3. **Configure the matching `pypi` GitHub environment.** PyPI's OIDC matching
   key includes the environment name; an OIDC token from a job without the
   environment will be rejected.

   - Go to <https://github.com/ozzy-labs/opshub/settings/environments>
   - Click **New environment**, name it `pypi` (must match step 2 exactly)
   - (Recommended) Under **Deployment protection rules**, enable
     **Required reviewers** and add yourself / the repo maintainers. This
     forces a manual approval click between `build` succeeding and `publish`
     running — a last human gate before the artifact reaches PyPI.
   - (Optional) Under **Deployment branches and tags**, restrict to
     **Selected tags** matching `v*.*.*`. This prevents a hypothetical
     `workflow_dispatch` from publishing off a non-tag ref.
   - Save protection rules.

That's it. Subsequent releases reuse the same configuration — no token
rotation, no secret management.

### 2. TestPyPI (recommended for first release only)

For the very first release, a TestPyPI dry-run de-risks the workflow before
touching real PyPI. Skip if confident.

- TestPyPI account: <https://test.pypi.org/account/register/>
- Pending publisher: same flow as PyPI, on `test.pypi.org`
- Temporarily override the workflow's publish step to target
  `https://test.pypi.org/legacy/`, push a `v0.0.0rcN` tag, verify, then
  revert.

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

The `build` job verifies tag vs `__version__` and fails fast on mismatch, so
a typo here is caught before anything reaches PyPI — but fixing it after the
tag is pushed is annoying (delete + re-push tag). Catch it locally first.

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

Pushing the tag fires `.github/workflows/release.yaml`. Two jobs run:

1. **`build`** — verifies `__version__` matches the tag, runs `uv build` to
   produce sdist + wheel under `dist/`, uploads as an artifact.
2. **`publish`** — gated on the `pypi` environment. Downloads the artifact
   and uploads to PyPI via `pypa/gh-action-pypi-publish@release/v1` using the
   OIDC token. No `PYPI_TOKEN` is read or referenced.

Watch progress:

- <https://github.com/ozzy-labs/opshub/actions/workflows/release.yaml>

If the `pypi` environment has required reviewers, `publish` blocks at
**Waiting for review** until a reviewer approves. Approve manually after
verifying the build artifact on the `build` job's Artifacts tab — open the
wheel filename and confirm it ends in `-<new_version>-py3-none-any.whl`.

### 4. Verify on PyPI

After the workflow completes:

```bash
# Wait ~30 seconds for PyPI CDN propagation, then:
uv tool install ozzylabs-opshub
opshub --version
# expected: opshub <new_version>

# Smoke test:
opshub init
opshub task create "Smoke test from PyPI install"
opshub task list
```

If `opshub --version` reports a stale version, your local `uv tool` cache is
in the way:

```bash
uv tool uninstall ozzylabs-opshub
uv tool install ozzylabs-opshub
```

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

The release-notes files live in `docs/release-notes-v<X.Y.Z>.md` so they stay
discoverable on the default branch even after the GitHub Release UI is
restructured.

### 6. Announce

For minor / major releases:

- [ ] Update any project social media / blog (if applicable)
- [ ] Notify users in any relevant communities

For patch releases:

- [ ] No announcement needed unless the patch closes a CVE or fixes a
      data-loss bug. `pip` / `uv` / `pipx` upgrade paths surface it
      automatically.

## Alternative: install from git source (no PyPI)

Users in air-gapped environments, or who want to install an unreleased fix
on `main`, can install directly from a git ref without PyPI involvement:

```bash
uv tool install git+https://github.com/ozzy-labs/opshub.git@v0.1.0
# or
uv tool install git+https://github.com/ozzy-labs/opshub.git    # HEAD of main
```

This is documented in the README under §Install. The package builds from
source, so the install is slightly slower than the pre-built wheel from
PyPI, but functionally identical.

## Troubleshooting

### Release workflow fails with "trusted publisher not found"

PyPI-side configuration drifted from the workflow. Re-check the Trusted
Publisher entry on PyPI matches the repo / workflow / environment exactly
(<https://pypi.org/manage/project/ozzylabs-opshub/settings/publishing/>).
Common mismatches:

- Workflow filename is `release.yaml` on disk but the publisher was
  registered with `release.yml`
- GitHub environment is `pypi` but the publisher was registered with
  `pypi-prod`
- Owner is `ozzy-labs` but the publisher was registered with a personal
  account that later transferred the repo
- PyPI Project Name is `ozzylabs-opshub` but the publisher was registered
  with `opshub` (the original name that PyPI rejected — must use the
  PEP 423 form throughout)

### Tag pushed but workflow didn't fire

Tags must match `v*.*.*` (SemVer). `v0.1.0` ✅, `0.1.0` (no `v`) ❌, `v0.1`
❌. Confirm via:

```bash
gh run list --workflow=release.yaml --limit 5
```

If the run isn't listed, the tag didn't match the filter. Push a new tag
with the correct shape; the bad tag can be deleted from GitHub afterwards.

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

### Need to yank a release

If a released version is broken:

1. Yank via PyPI Web UI:
   <https://pypi.org/manage/project/ozzylabs-opshub/release/X.Y.Z/> →
   **Options** → **Yank release**
2. Ship a patched release (`X.Y.Z+1`) following the per-release flow above.

Yanking removes the version from default-resolved installs
(`pip install ozzylabs-opshub` / `uv tool install ozzylabs-opshub`) but lets
explicit pins (`pip install ozzylabs-opshub==X.Y.Z`) still resolve, so
existing lockfiles don't suddenly break. Always pair a yank with a patch
release ASAP.

### `publish` job stuck on "Waiting for review"

Expected behaviour when the `pypi` environment has required reviewers — this
is the manual gate. Open the workflow run page and click **Review
deployments → pypi → Approve and deploy**. If you don't see the button,
you're not in the reviewer list on the environment; ask a maintainer.

## References

- [`.github/workflows/release.yaml`](../.github/workflows/release.yaml) — the
  workflow this runbook describes
- [`CHANGELOG.md`](../CHANGELOG.md) — release history
- [`docs/upgrading.md`](upgrading.md) — end-user version migration notes
- [ADR-0001 §Updates](adr/0001-python-stack.md#updates) — distribution +
  naming rationale
- [PyPI Trusted Publishers docs](https://docs.pypi.org/trusted-publishers/)
- [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) /
  [SemVer](https://semver.org/spec/v2.0.0.html) /
  [PEP 423 (naming conventions)](https://peps.python.org/pep-0423/)
