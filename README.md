# arcuru/actions

Shared GitHub Actions building blocks for the [`arcuru`](https://github.com/arcuru) repos.

Two kinds of things live here:

- **Composite actions** under `.github/actions/` — small reusable steps callable from any workflow.
- **Reusable workflows** under `.github/workflows/` — whole workflows callable via `workflow_call`, mostly the dependency-update automation.

Pinning is by full commit SHA, with a trailing version comment that matches the latest release tag.

## Composite actions

| Action | Purpose |
|---|---|
| [`setup-nix`](.github/actions/setup-nix/action.yml) | Install Nix with `nix-installer-action`, optionally enable Magic Nix Cache, optionally configure a custom binary cache (substituters / trusted keys / push), install `nix-fast-build`. |
| [`setup-deps-branch`](.github/actions/setup-deps-branch/action.yml) | Create a dated `deps/<name>-YYYY-MM-DD` branch from HEAD. Outputs the full branch name. |
| [`commit-and-pr`](.github/actions/commit-and-pr/action.yml) | Stage, commit, push, and open a PR with `dependencies` + `on-hold` labels and a `hold-until: YYYY-MM-DD` body marker. Each run gets its own dated branch and PR; earlier runs' PRs stay open. |
| [`scan-pins`](.github/actions/scan-pins/action.yml) | Discover SHA-pinned `uses:` references across workflows and composite-action manifests, as JSON. Scans the working directory, or any remote ref via the API without a checkout. |
| [`verify-pins`](.github/actions/verify-pins/action.yml) | Check scanned pins for re-pointed upstream tags and published security advisories. |

Reference one from any workflow:

```yaml
- uses: arcuru/actions/.github/actions/setup-nix@<sha>  # v0.1.0
```

The trailing version comment is required — the `actions-update` workflow uses it to find the next release tag.

### `setup-nix` inputs

| Input | Default | Purpose |
|---|---|---|
| `enable-magic-cache` | `"true"` | Toggle the DeterminateSystems Magic Nix Cache step. Set to `"false"` if you only want a custom cache. |
| `extra-substituters` | `""` | Newline-separated substituter URLs appended to the Nix config (e.g. `https://cache.eidetica.dev`). |
| `extra-trusted-public-keys` | `""` | Newline-separated trusted public keys for those substituters. Required when `extra-substituters` is set. |
| `signing-key` | `""` | Nix store signing key PEM body. When paired with `push-target`, the action exposes a `push-args` output containing `nix-fast-build` flags for cache push. |
| `push-target` | `""` | Cache push destination URL (e.g. `s3://my-cache?region=auto&endpoint=…`). |

Output `push-args` is empty unless both `signing-key` and `push-target` are set; pass it to `nix-fast-build` to push built paths back into the cache in the same step.

## Reusable workflows

All six follow the same calling convention: each consumer repo ships a thin wrapper that owns the schedule, the wrapper calls the reusable workflow here.

| Workflow | Trigger in wrapper | Secrets | What it does |
|---|---|---|---|
| [`cargo-update`](.github/workflows/cargo-update.yml) | `schedule` (monthly) | `PAT_TOKEN` | `cargo update` → PR for `Cargo.lock` |
| [`flake-update`](.github/workflows/flake-update.yml) | `schedule` (monthly) | `PAT_TOKEN` | `nix flake update` → PR for `flake.lock` with per-input compare URLs |
| [`actions-update`](.github/workflows/actions-update.yml) | `schedule` (monthly) | `PAT_TOKEN` | Bump every SHA-pinned `uses:` in `.github/workflows/` to the latest release tag |
| [`security-audit`](.github/workflows/security-audit.yml) | `schedule` (daily) | — | `cargo deny check advisories`; opens a tracking issue on hit, closes it on resolution |
| [`dependency-hold`](.github/workflows/dependency-hold.yml) | `pull_request` | — | Fails any PR on a `deps/*` branch that still has the `on-hold` label |
| [`update-hold`](.github/workflows/update-hold.yml) | `schedule` (daily) | `PAT_TOKEN` | Removes `on-hold` once the PR's `hold-until: YYYY-MM-DD` marker has passed **and** the PR's pinned actions verify |
| [`actions-audit`](.github/workflows/actions-audit.yml) | `schedule` (daily) | — | Re-resolves every pinned action against upstream tags and the advisory database, and audits the workflows themselves with `zizmor`; opens a tracking issue on hit, closes it on resolution, and uploads the `zizmor` findings as SARIF to code scanning |
| [`codeberg-mirror`](.github/workflows/codeberg-mirror.yml) | `push` | `ssh-private-key` | Mirror the caller repo to a Codeberg/Forgejo destination over SSH with ed25519 host-key pinning |

### Common inputs

Every reusable workflow accepts these optional overrides (defaults preserve the simplest behaviour):

| Input | Default | Purpose |
|---|---|---|
| `runs-on` | `ubuntu-latest` | Override the runner. Example: `ubicloud-standard-2`. |
| `environment` | `""` | Attach the job to a GitHub Actions environment so approval rules and env-scoped secrets apply. |

Nix-using workflows (cargo-update, flake-update, actions-update, security-audit) additionally accept:

| Input | Default | Purpose |
|---|---|---|
| `enable-magic-cache` | `"true"` | Toggle DeterminateSystems Magic Nix Cache. |
| `extra-substituters` | `""` | Newline-separated extra substituter URLs. |
| `extra-trusted-public-keys` | `""` | Newline-separated trusted public keys. |

### Wrapper examples

Drop these into the consumer's `.github/workflows/` directory. Replace `<sha>` with the current `arcuru/actions` commit SHA and `vX.Y.Z` with the matching tag.

**`cargo-update.yml`** (also the pattern for `flake-update`, `actions-update`):

```yaml
name: "Deps: Cargo Update"

on:
  schedule:
    - cron: "0 4 1 * *"
  workflow_dispatch:
    inputs:
      hold_days:
        description: "Days to hold before allowing merge"
        required: false
        default: "7"
        type: string

permissions:
  contents: write
  pull-requests: write

jobs:
  cargo-update:
    uses: arcuru/actions/.github/workflows/cargo-update.yml@<sha>  # vX.Y.Z
    with:
      hold-days: ${{ inputs.hold_days || '7' }}
    secrets: inherit
```

The caller's top-level `permissions:` must grant at least what the reusable workflow's job declares (`contents: write` + `pull-requests: write`); the called job is capped by the caller's grant.

`secrets: inherit` works whether `PAT_TOKEN` is a repo secret or scoped to the environment passed via the `environment:` input. The explicit form `secrets: PAT_TOKEN: ${{ secrets.PAT_TOKEN }}` only resolves repo-scoped secrets — env-scoped secrets read as empty in caller scope and the reusable workflow's `actions/checkout` fails with `Input required and not supplied: token`.

**Repo using a custom Nix cache** (eidetica-style):

```yaml
jobs:
  cargo-update:
    uses: arcuru/actions/.github/workflows/cargo-update.yml@<sha>  # vX.Y.Z
    with:
      runs-on: ubicloud-standard-2
      environment: automation
      enable-magic-cache: "false"
      extra-substituters: https://cache.eidetica.dev
      extra-trusted-public-keys: cache.eidetica.dev-1:eND5gRJlbnool3ZLCWT2H8kkygWS8JcsU76HYXbWPBI=
    secrets: inherit
```

**`security-audit.yml`** (schedule-triggered, no secrets):

```yaml
name: "Deps: Security Audit"

on:
  schedule:
    - cron: "0 6 * * *"
  workflow_dispatch:

permissions:
  contents: read

jobs:
  audit:
    uses: arcuru/actions/.github/workflows/security-audit.yml@<sha>  # vX.Y.Z
    secrets: inherit
```

**`actions-audit.yml`** (schedule-triggered, no secrets, but needs the code-scanning grant):

```yaml
name: "Deps: Actions Audit"

on:
  schedule:
    - cron: "0 6 * * *"
  workflow_dispatch:

permissions:
  contents: read
  issues: write
  security-events: write

jobs:
  audit:
    uses: arcuru/actions/.github/workflows/actions-audit.yml@<sha>  # vX.Y.Z
    secrets: inherit
```

`security-events: write` is what lets the job upload the `zizmor` SARIF to code scanning. A reusable workflow's job permissions are capped by the caller's grant, so a caller that omits it does not merely skip the upload — GitHub refuses to start the run and the job ends as `startup_failure`.

On a **private** repo the SARIF upload additionally needs `actions: read`; add it to the caller's `permissions:` block there. Public callers do not.

**`dependency-hold.yml`** (PR-triggered):

```yaml
name: "Deps: Hold Gate"

on:
  pull_request:
    branches: ["main"]
    types: [opened, synchronize, reopened, labeled, unlabeled]

permissions:
  contents: read
  pull-requests: read

jobs:
  hold:
    uses: arcuru/actions/.github/workflows/dependency-hold.yml@<sha>  # vX.Y.Z
    secrets: inherit
```

**`update-hold.yml`** (schedule-triggered; needs `PAT_TOKEN`, see below):

```yaml
name: "Deps: Hold Expiry"

on:
  schedule:
    - cron: "0 7 * * *"
  workflow_dispatch:

permissions:
  contents: read

jobs:
  update-hold:
    uses: arcuru/actions/.github/workflows/update-hold.yml@<sha>  # vX.Y.Z
    secrets: inherit
```

### Wiring the hold gate as a required check

The hold gate only blocks a merge if branch protection requires it, and the
context name is **not** `Dependency Hold`. A reusable workflow's check is
reported as `<caller job id> / <called job name>`, so with the wrapper above
the required status check to configure is:

```
hold / Dependency Hold
```

Requiring the bare `Dependency Hold` instead silently blocks *every* PR in the
repo: that context is never reported, so it sits permanently missing and only
an admin bypass can merge.

`update-hold` needs `PAT_TOKEN` because it removes the `on-hold` label, and
that `unlabeled` event must re-trigger the hold gate so the gate can replace
its own failing check. Events authored by `GITHUB_TOKEN` do not trigger other
workflows, so a `GITHUB_TOKEN` removal leaves the check red and the PR blocked
even though the hold has expired.

**`codeberg-mirror.yml`** (push-triggered):

```yaml
name: "Codeberg Sync"

on:
  push:
    branches: [main]

permissions:
  contents: read

jobs:
  mirror:
    uses: arcuru/actions/.github/workflows/codeberg-mirror.yml@<sha>  # vX.Y.Z
    with:
      environment: mirror
      destination: git@codeberg.org:arcuru/<repo>.git
    secrets:
      ssh-private-key: ${{ secrets.GIT_SSH_PRIVATE_KEY }}
```

`codeberg-mirror` accepts `host`, `host-key`, and `host-key-algorithm` overrides for non-default forges or rotated keys. The destination repo must already exist on Codeberg; the deploy key for `secrets.ssh-private-key` must have write access. The mirror push uses `+refs/remotes/origin/*:refs/heads/*` with `--prune` scoped to that namespace, so deleted GitHub branches propagate to Codeberg without touching anything outside `refs/heads/*`.

### Prerequisites in the consumer repo

- `cargo-update` and `security-audit` need `nix develop --command cargo …` to work — i.e. a `flake.nix` with `cargo` (and for security-audit, `cargo-deny`) in the dev shell.
- `flake-update` needs `flake.nix` + `flake.lock`.
- `security-audit` needs `.config/deny.toml`.
- `cargo-update` / `flake-update` / `actions-update` need a fine-grained `PAT_TOKEN` with `contents:write` + `pull-requests:write` on the calling repo. Store it as either a repo secret or as an environment secret scoped to the env passed via the `environment:` input — both work with `secrets: inherit`. (The default `GITHUB_TOKEN` can't trigger downstream workflow runs, which would silently break the deps-hold check.)
- `actions-audit` needs `security-events: write` in the caller's `permissions:` block, on top of `contents: read` + `issues: write`, plus `actions: read` if the calling repo is private. Code scanning must be available on the repo for the SARIF upload to land.
- The `on-hold` and `dependencies` labels need to exist in the repo (the deps PRs use them).
- `Allow GitHub Actions to create and approve pull requests` must be enabled in Settings → Actions → General.

## Security model

- **No secrets stored here.** Every secret a workflow needs is passed in by the caller. The consumer's repo settings are the only place credentials live.
- **`PAT_TOKEN` is scoped per consumer**, not shared. Each repo that wants the PR-creating deps automation needs its own fine-grained PAT covering only that repo, with the minimum scopes (`contents:write` + `pull-requests:write`).
- **All `uses:` lines pin a full commit SHA**, with the version comment used by `actions-update` for bumps. Mutable tags like `@v1` are forbidden.
- **No `${{ }}` interpolation inside `run:` blocks**: untrusted values reach the shell via `env:` only. This is enforced inside this repo and is the rule callers should follow too.
- **Pins are verified, not just recorded.** `actions-audit` re-resolves every pinned immutable tag daily, and `update-hold` re-verifies a PR before releasing its hold. See [Pin verification](#pin-verification).
- **Workflow definitions are audited too.** The same daily `actions-audit` run puts `zizmor` over every workflow and composite action, covering the posture problems a pin check says nothing about. See [Workflow auditing](#workflow-auditing-zizmor).
- **Fork-fenced**: every workflow's privileged job has `if: github.repository_owner == 'arcuru'`. Forks running re-enabled copies of the workflow no-op cleanly.

## Pin verification

Pinning by SHA stops a moved tag from changing what CI runs. It does not tell you
when a tag *has* moved — and that is the signal worth having.

Every recent GitHub Actions supply-chain compromise (tj-actions/changed-files,
reviewdog/action-setup, aquasecurity/trivy-action) worked the same way: the
attacker force-pushed existing release tags to malicious commits. A SHA-pinned
consumer keeps running the original commit, so it is protected — but it is also
unaware. The divergence between "the SHA I pinned" and "the commit that tag serves" is
visible immediately from public API data, days before an advisory is
published.

Three checks run over every discovered reference:

**Pin integrity.** Re-resolve the version comment upstream and compare it to the
pinned SHA. Severity depends on what kind of tag it is, which is the difference
between a usable signal and constant noise:

| Comment | Class | A mismatch means |
|---|---|---|
| `v7.0.0` | immutable | **critical** — a release tag was re-pointed or deleted |
| `v2`, `v2.9` | mutable | informational — floating tags are republished by design |
| `master`, `main` | branch | **critical** if the name is not a real branch, or the commit is not reachable from it |
| anything else | unknown | **warning** — nothing resolvable to compare against |
| *(not a SHA at all)* | unpinned | **critical** — `@v4`/`@main` can be replaced upstream with no change here |
| *(none)* | unverifiable | warning — nothing to compare against |

Floating tags really do move: `Swatinem/rust-cache@v2` and
`codecov/codecov-action@v5` have both been republished. Failing on those would
make the audit unreadable within a week, so only exact semver tags are treated as
immutable.

Branch-tracking pins name a branch where a version would go (`# main`,
`# master`). There is no fixed SHA to expect, but two things are checkable:
that the name is a real branch and not a tag (the compare endpoint resolves
tags too, so a tag name would otherwise pass as though it were a branch), and
that the commit is reachable from it. A commit that never landed on the branch,
because it was squashed away or force-pushed over, resolves and vanishes when
that line is garbage collected. This repo's own references track `main` this way:
they are an implementation detail of the reusable workflows, not part of the
released interface, so coupling them to release tags only creates a bootstrap
problem every time a new action is added.

**Reference resolvability.** Confirm the action or reusable workflow actually
exists at the pinned commit. A pin can be internally consistent — the SHA really
is what its tag resolves to — while naming a path that is not present there,
which otherwise fails only at run time.

**Advisory lookup.** Query the GitHub Advisory Database. An exact version is
queried as `OWNER/REPO@VERSION`, so a reference on a patched release is not
reported against an advisory it is not subject to. Without a usable version only
the repository-wide query is available, and its results are warnings rather than
criticals, because they may describe a release the reference is not on.

These checks are consumed in two places, differing in what they look at rather
than in what they detect. `actions-audit` runs them daily over every pin on the
default branch, so a pin stays covered for as long as it is in the tree — which
is what catches a tag re-pointed, or an advisory published, long after the PR
that introduced the pin was merged. `update-hold` runs them against a PR's head
before releasing its hold, so aging a dependency PR actually inspects it instead
of only waiting.

### What this does not cover

The strong guarantee applies to exact-version pins. A `v7.0.0` comment fixes a
commit that can be re-checked forever, so tampering shows up. Floating tags and
branch pins have no fixed SHA to expect, so drift there is reported but never
fails — an attacker who force-pushes `v9`, or the branch a pin tracks, *before*
the updater next runs produces a reference that verifies as consistent. Pinning
to exact versions is what buys the protection; the other classes get
resolvability and advisory coverage only.

No check here detects a backdoor committed by a legitimate maintainer into a
normal release: no tag moves and no advisory exists. These detect tag tampering,
references that cannot resolve, unpinned references, and published advisories.

An upstream lookup that cannot be completed is reported as a warning, never as a
pass and never as a compromise, so a rate limit neither certifies a reference nor
raises a false alarm across every pin at once.

## Workflow auditing (zizmor)

Pin verification asks whether a reference still points where it claimed to. It
says nothing about what the workflow around that reference does. The same daily
`actions-audit` run therefore also puts [`zizmor`](https://github.com/zizmorcore/zizmor)
over every workflow and composite action in the tree, covering workflow security
posture: `${{ }}` interpolation reaching a `run:` block (template injection),
dangerous triggers such as `pull_request_target`, credentials left behind in a
checkout's git config, over-broad `permissions:`, unpinned `uses:` references,
and similar definition-level problems.

`zizmor` runs with its online audits enabled, which query the GitHub API rather
than reading the file alone — flagging references to actions with published
advisories and commits that are not reachable from the repository they appear
to come from.

Findings are uploaded as SARIF, so they land in the repo's code scanning alerts
with per-line annotations and their own dismissal state, instead of being flattened
into the tracking issue. That upload is the reason callers must grant
`security-events: write`.

**It does not replace tag re-resolution.** No `zizmor` audit reads the trailing
`# vX.Y.Z` comment on a pin and re-resolves it upstream, so a release tag
force-pushed to a malicious commit — the shape every recent Actions compromise
actually took — is invisible to it. Detecting that remains the job of the
`scan-pins` / `verify-pins` pass described above, and stays the more important
half of this workflow. The two are complementary: one checks that references
still mean what they said, the other checks what the workflows do with them.

## Releases

Releases are tagged from `main` as `vX.Y.Z`. Consumers should pin a full SHA *with* a matching version comment so the `actions-update` workflow can bump them.

Breaking changes (input renames, removed secrets, schema changes) bump the major version. Most updates are SHA-pin bumps inside the reusable workflows or composite-action body changes — those don't require consumers to act.

## Known limitations

- Pin verification only covers refs pinned to a SHA *with* a version comment. A pin with no comment cannot be compared against anything and is reported as a warning rather than checked.
- Pin verification cannot detect a backdoor introduced by a legitimate maintainer in a normal release — no tag moves and no advisory exists. It detects tag tampering and published advisories, which is what the recent Actions compromises actually looked like.
- `actions-audit` and `update-hold` reference the `scan-pins` / `verify-pins` actions by SHA. When cutting a release that changes those actions, re-pin those references to the release commit.
- The `cargo-update` workflow assumes a single-package crate at the repo root (`Cargo.lock` only). Workspaces with multiple lockfiles need an extension.
- `flake-update`'s per-input compare URL only works when the input source is a `github:` flake ref. Other types (`git+ssh://`, `path:`, `tarball:`) fall back to bare-SHA display.

## License

MIT. See [LICENSE](LICENSE).
