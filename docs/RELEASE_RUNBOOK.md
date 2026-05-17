# Release Runbook

How to cut an OpsHub release. `v0.1.0` is the first public release; subsequent
patch / minor releases follow the same flow.

**Distribution**: PyPI under the name **`ozzylabs-opshub`** (PEP 423
`<owner>-<package>` form, because PyPI has no namespace concept and the bare
`opshub` name was unavailable). The CLI command stays `opshub`. See
[ADR-0001 §Updates](adr/0001-python-stack.md#updates) for the naming
rationale.

**Release automation**: [release-please](https://github.com/googleapis/release-please)
drives the release pipeline. Conventional Commits to `main` (e.g. `feat:` /
`fix:` / `perf:`) accumulate in a long-lived "release PR"
(`chore(main): release vX.Y.Z`) that release-please maintains automatically.
Merging that PR creates the tag, the GitHub Release, and triggers PyPI
publish in one step — see `.github/workflows/release-please.yaml`.

`.github/workflows/release.yaml` is the **emergency escape hatch** for
manually-pushed tags (the GitHub Actions cascading-trigger limitation means
release-please-created tags don't fire it; operator-pushed tags do). It is
mostly dormant.

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

## Per-release flow (release-please-driven, **primary**)

### 1. Land changes via Conventional Commits

All commits to `main` follow [Conventional Commits](https://www.conventionalcommits.org/).
release-please reads commit subjects to compute the next version + populate
CHANGELOG:

| Type | Effect |
|---|---|
| `feat: ...` | Minor bump in `0.x` line (capped to minor by `bump-minor-pre-major`) |
| `fix: ...` | Patch bump |
| `perf: ...` | Patch bump |
| `feat!: ...` or `BREAKING CHANGE:` footer | Major bump (still capped to minor in 0.x by `bump-minor-pre-major`) |
| `docs: ...` / `refactor: ...` | Shown in CHANGELOG, no version bump |
| `chore: ...` / `test: ...` / `ci: ...` / `build: ...` / `style: ...` | Hidden from CHANGELOG, no version bump |

Sections + visibility configured in `release-please-config.json`.

### 2. Review the release PR

After any commit lands on `main`, release-please-action runs and either
opens or updates a "release PR":

- Title: `chore(main): release X.Y.Z`
- Body: auto-generated CHANGELOG diff for the upcoming release
- Files touched: `pyproject.toml` (version), `src/opshub/__init__.py`
  (`__version__`), `CHANGELOG.md` (new entry), `.release-please-manifest.json`

Inspect the diff. Common review items:

- Is the proposed version correct? release-please uses SemVer arithmetic
  based on Conventional Commit types. Override via
  [release-as commit annotation](https://github.com/googleapis/release-please/blob/main/docs/manifest-releaser.md#how-do-i-change-the-version-number)
  if needed.
- Does the CHANGELOG entry read well? Edit the PR's CHANGELOG entry directly
  if a `feat:` subject was too terse — release-please respects manual edits.
- Are there release notes worth promoting to a dedicated
  `docs/release-notes-v<X.Y.Z>.md` file? For minor / major releases, write
  one and link it in the PR description. release-please will use the PR
  body when creating the GitHub Release.

### 3. Merge the release PR

When ready: **Squash and merge** the release PR. This triggers
`.github/workflows/release-please.yaml` again, which:

1. Detects that `release_created: true` (the release PR was a release-creating
   merge)
2. Creates the git tag (`vX.Y.Z`) and the GitHub Release in one atomic step
3. Triggers the `publish` job in the same workflow → builds the wheel +
   uploads to PyPI via Trusted Publishers (OIDC)

The `pypi` environment's required-reviewers gate (if configured) holds the
publish job at **Waiting for review** until manual approval. Approve after
sanity-checking the `build` step's wheel filename.

Watch progress:

- <https://github.com/ozzy-labs/opshub/actions/workflows/release-please.yaml>

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

### 5. GitHub Release (auto-created by release-please)

release-please-action creates the GitHub Release atomically with the tag.
The Release body is the CHANGELOG diff release-please derived from the
release PR. For minor / major releases that warrant a longer narrative,
either:

- Edit the GitHub Release post-hoc to paste a dedicated
  `docs/release-notes-v<X.Y.Z>.md` body (the docs file stays in-repo for
  discoverability), or
- Land the dedicated release notes in the same release PR (put the file
  in `docs/release-notes-v<X.Y.Z>.md` before merging) and reference it from
  the GitHub Release body.

The release-notes files live in `docs/release-notes-v<X.Y.Z>.md` so they
stay discoverable on the default branch even after the GitHub Release UI is
restructured.

### 6. Announce

For minor / major releases:

- [ ] Update any project social media / blog (if applicable)
- [ ] Notify users in any relevant communities

For patch releases:

- [ ] No announcement needed unless the patch closes a CVE or fixes a
      data-loss bug. `pip` / `uv` / `pipx` upgrade paths surface it
      automatically.

## v0.1.0 special case (first release)

The v0.1.0 entry in `CHANGELOG.md` was hand-crafted with Phase 1-8 narrative
*before* release-please was wired in. The `.release-please-manifest.json`
bootstraps at `0.1.0`, so release-please will manage everything from v0.2.0
onwards. v0.1.0 itself ships via the manual tag-push path (`release.yaml`
escape hatch — see below) since the release PR concept doesn't apply
retroactively.

For v0.1.0 specifically:

```bash
git switch main && git pull --ff-only origin main

# The version + CHANGELOG are already correct (PRs #151, #156, #158)
git tag -a v0.1.0 -m "Release v0.1.0"
git push origin v0.1.0
# → release.yaml fires (manual tag push, not GITHUB_TOKEN-created)
# → build + publish to PyPI via OIDC

# Or simulate the release-please flow:
# Create a release PR manually with version + CHANGELOG already in place,
# merge it, and let release-please-action take over from v0.2.0.
```

## Emergency manual release (escape hatch)

If release-please is broken (action failure, repo permissions issue, etc.)
and a release is urgent, the legacy `.github/workflows/release.yaml`
provides a manual fallback:

```bash
# 1. Bump version + CHANGELOG manually
vi pyproject.toml src/opshub/__init__.py CHANGELOG.md
git commit -am "chore(release): manual bump to v${new_version}"
git push origin main

# 2. Push tag
git tag -a "v${new_version}" -m "Release v${new_version}"
git push origin "v${new_version}"
# → release.yaml fires (operator-pushed tag IS triggered, unlike
#   release-please-created tags which use GITHUB_TOKEN and don't cascade)
```

This path produces an identical artifact to the release-please flow but
skips the release PR review step. Only use for emergencies — the release PR
is the audit trail.

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
