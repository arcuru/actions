# arcuru/actions

Shared GitHub Actions building blocks for the [`arcuru`](https://github.com/arcuru) repos.

Two kinds of things live here:

- **Composite actions** under `.github/actions/` — small reusable steps callable from any workflow.
- **Reusable workflows** under `.github/workflows/` — whole workflows callable via `workflow_call`, mostly the dependency-update automation.

Pinning is by full commit SHA, with a trailing version comment that matches the latest release tag.

## Composite actions

| Action | Purpose |
|---|---|
| [`setup-nix`](.github/actions/setup-nix/action.yml) | Install Nix with `nix-installer-action`, enable Magic Nix Cache, install `nix-fast-build`. |
| [`setup-deps-branch`](.github/actions/setup-deps-branch/action.yml) | Create a new `deps/<name>` branch, or rebase an existing one onto `main`. Falls back to `reset --hard` on rebase conflict. |
| [`commit-and-pr`](.github/actions/commit-and-pr/action.yml) | Stage, commit, push, and open or update a PR with `dependencies` + `on-hold` labels and a `hold-until: YYYY-MM-DD` body marker. |

Reference one from any workflow:

```yaml
- uses: arcuru/actions/.github/actions/setup-nix@<sha>  # v0.1.0
```

The trailing version comment is required — the `actions-update` workflow uses it to find the next release tag.

## Reusable workflows

All six follow the same calling convention: each consumer repo ships a thin wrapper that owns the schedule, the wrapper calls the reusable workflow here.

| Workflow | Trigger in wrapper | Secrets | What it does |
|---|---|---|---|
| [`cargo-update`](.github/workflows/cargo-update.yml) | `schedule` (monthly) | `PAT_TOKEN` | `cargo update` → PR for `Cargo.lock` |
| [`flake-update`](.github/workflows/flake-update.yml) | `schedule` (monthly) | `PAT_TOKEN` | `nix flake update` → PR for `flake.lock` with per-input compare URLs |
| [`actions-update`](.github/workflows/actions-update.yml) | `schedule` (monthly) | `PAT_TOKEN` | Bump every SHA-pinned `uses:` in `.github/workflows/` to the latest release tag |
| [`security-audit`](.github/workflows/security-audit.yml) | `schedule` (daily) | — | `cargo deny check advisories`; opens a tracking issue on hit, closes it on resolution |
| [`dependency-hold`](.github/workflows/dependency-hold.yml) | `pull_request` | — | Fails any PR on a `deps/*` branch that still has the `on-hold` label |
| [`update-hold`](.github/workflows/update-hold.yml) | `schedule` (daily) | — | Removes `on-hold` once the PR's `hold-until: YYYY-MM-DD` marker has passed |

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
  contents: read

jobs:
  cargo-update:
    uses: arcuru/actions/.github/workflows/cargo-update.yml@<sha>  # vX.Y.Z
    with:
      hold-days: ${{ inputs.hold_days || '7' }}
    secrets:
      PAT_TOKEN: ${{ secrets.PAT_TOKEN }}
```

**`security-audit.yml`** (also the pattern for `update-hold` — schedule-triggered, no secrets):

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

**`dependency-hold.yml`** (PR-triggered):

```yaml
name: "Deps: Hold Gate"

on:
  pull_request:
    branches: ["main"]
    types: [opened, synchronize, reopened, labeled, unlabeled]

permissions:
  contents: read

jobs:
  hold:
    uses: arcuru/actions/.github/workflows/dependency-hold.yml@<sha>  # vX.Y.Z
    secrets: inherit
```

### Prerequisites in the consumer repo

- `cargo-update` and `security-audit` need `nix develop --command cargo …` to work — i.e. a `flake.nix` with `cargo` (and for security-audit, `cargo-deny`) in the dev shell.
- `flake-update` needs `flake.nix` + `flake.lock`.
- `security-audit` needs `.config/deny.toml`.
- `cargo-update` / `flake-update` / `actions-update` need a fine-grained `PAT_TOKEN` repo-level secret with `contents:write` + `pull-requests:write` on the calling repo. (The default `GITHUB_TOKEN` can't trigger downstream workflow runs, which would silently break the deps-hold check.)
- The `on-hold` and `dependencies` labels need to exist in the repo (the deps PRs use them).
- `Allow GitHub Actions to create and approve pull requests` must be enabled in Settings → Actions → General.

## Security model

- **No secrets stored here.** Every secret a workflow needs is passed in by the caller. The consumer's repo settings are the only place credentials live.
- **`PAT_TOKEN` is scoped per consumer**, not shared. Each repo that wants the PR-creating deps automation needs its own fine-grained PAT covering only that repo, with the minimum scopes (`contents:write` + `pull-requests:write`).
- **All `uses:` lines pin a full commit SHA**, with the version comment used by `actions-update` for bumps. Mutable tags like `@v1` are forbidden.
- **No `${{ }}` interpolation inside `run:` blocks**: untrusted values reach the shell via `env:` only. This is enforced inside this repo and is the rule callers should follow too.
- **Fork-fenced**: every workflow's privileged job has `if: github.repository_owner == 'arcuru'`. Forks running re-enabled copies of the workflow no-op cleanly.

## Releases

Releases are tagged from `main` as `vX.Y.Z`. Consumers should pin a full SHA *with* a matching version comment so the `actions-update` workflow can bump them.

Breaking changes (input renames, removed secrets, schema changes) bump the major version. Most updates are SHA-pin bumps inside the reusable workflows or composite-action body changes — those don't require consumers to act.

## Known limitations

- `actions-update`'s regex parses the `OWNER/REPO@SHA # vX.Y.Z` shape used for everything but doesn't yet handle the path-prefixed `OWNER/REPO/PATH@SHA # vX.Y.Z` shape this repo uses for its own composite-action references inside the reusable workflows. Path-prefixed pins must be bumped by hand until the regex grows the second form. Filed as TODO inside the reusable `actions-update.yml`.
- The `cargo-update` workflow assumes a single-package crate at the repo root (`Cargo.lock` only). Workspaces with multiple lockfiles need an extension.
- `flake-update`'s per-input compare URL only works when the input source is a `github:` flake ref. Other types (`git+ssh://`, `path:`, `tarball:`) fall back to bare-SHA display.

## License

MIT. See [LICENSE](LICENSE).
