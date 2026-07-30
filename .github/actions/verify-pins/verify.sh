#!/usr/bin/env bash
#
# Verify `uses:` references against upstream.
#
# Reads the JSON produced by scan-pins on stdin (or $1) and runs three
# independent checks over each distinct reference.
#
# Check A - reference integrity. Re-resolve the version comment upstream and
# compare it to the pinned SHA. Severity depends on the reference:
#
#   unpinned   Not pinned to a commit at all. Reported, never skipped: a
#              reference that vanishes from the scan is indistinguishable from
#              a clean repository, which is how a change that removes pinning
#              would otherwise pass verification.
#   immutable  An exact semver tag must always resolve to the same commit.
#              If it resolves elsewhere, or has vanished, the tag was
#              re-pointed upstream. That is the signature of every recent
#              Actions supply-chain compromise (tj-actions, reviewdog, Trivy):
#              the pin holds the good commit while the tag serves a
#              malicious one. Detecting this needs no advisory to exist yet,
#              which is why it fires days before the advisory database does.
#   mutable    Floating tags (v2, v5) are republished by design, so drift is
#              reported for visibility but never fails.
#   branch     Branch pins move by design, so there is no fixed SHA to expect.
#              What is checkable is that the pinned commit is reachable from
#              the branch it claims to track. A commit that never landed there
#              resolves and disappears when that line is collected.
#   unknown    A comment naming neither a version nor a resolvable ref. Warned
#              about rather than guessed at, so that a typo cannot quietly
#              demote a pin out of the only class that fails.
#   none       No version comment, so there is nothing to compare against.
#
# Check B - advisory lookup. Query the GitHub Advisory Database. An exact
# version is queried as OWNER/REPO@VERSION so a pin on a patched release is not
# reported forever against an advisory it is not subject to.
#
# Check C - reference resolvability. Confirm the referenced action or reusable
# workflow exists at the pinned commit. A pin can be internally consistent (the
# SHA genuinely is what the tag resolves to) while naming a path not present
# there, which otherwise fails only at run time.
#
# Check D - self-reference content drift. A reference naming the repository
# being audited is internal coupling, not a dependency: a reusable workflow's
# relative `./` paths resolve against the caller's workspace, so this repo's
# own actions must be named absolutely. For those, "the pinned SHA is old" is
# not a finding — every commit to this repository makes it older. The
# answerable question is whether the referenced action's content differs
# between the pinned commit and the audited tree, i.e. whether a workflow is
# running a stale copy of an action that lives beside it. Reported as a warning
# because it describes internal lag, not an upstream compromise. Repositories
# that merely consume these actions have no self-references, so the check finds
# nothing there.
#
# Failure handling is deliberate: a lookup that could not be completed is a
# warning, never a clean pass and never a compromise. A rate limit must not
# read as "verified", nor cry "force-push attack" across every pin at once.
#
# Exit code is always 0; callers branch on the `status` output instead.

set -uo pipefail

INPUT="${1:--}"
PINS=$(cat "$INPUT")
REPORT_FILE="${RUNNER_TEMP:-/tmp}/verify-pins-report.md"

# The repository this run is auditing, and the tree self-references are compared
# against. A reference naming this repository is a self-reference (see Check D).
# HEAD_REF names that tree when it is not the commit this run is at. Both are
# empty outside Actions, which disables Check D rather than guessing.
SELF_REPO="${GITHUB_REPOSITORY:-}"
SELF_HEAD="${HEAD_REF:-${GITHUB_SHA:-}}"

CRITICAL=0
WARNING=0
INFO=0
CHECKED=0

CRIT_LINES=""
WARN_LINES=""
INFO_LINES=""

emit() { # severity, markdown line
  case "$1" in
    critical) CRIT_LINES="${CRIT_LINES}$2"$'\n'; CRITICAL=$((CRITICAL + 1)) ;;
    warning)  WARN_LINES="${WARN_LINES}$2"$'\n'; WARNING=$((WARNING + 1)) ;;
    info)     INFO_LINES="${INFO_LINES}$2"$'\n'; INFO=$((INFO + 1)) ;;
  esac
}

# GET a path from the API, separating "upstream says no" from "we could not ask".
# Prints the body on success.
#   0 = ok, 1 = definitively absent (404), 2 = request could not be completed
gh_get() {
  local path="$1" body err rc
  if ! err=$(mktemp); then
    return 2
  fi
  body=$(gh api "$path" 2>"$err")
  rc=$?
  if [ "$rc" -eq 0 ]; then
    rm -f "$err"
    printf '%s' "$body"
    return 0
  fi
  if grep -qiE '(HTTP 404|Not Found)' "$err"; then
    rm -f "$err"
    return 1
  fi
  rm -f "$err"
  return 2
}

# Resolve a tag to the commit it points at, following annotated tags through to
# the underlying commit. Same exit convention as gh_get.
resolve_tag() {
  local repo="$1" tag="$2" body sha type rc
  body=$(gh_get "repos/${repo}/git/ref/tags/${tag}"); rc=$?
  [ "$rc" -eq 0 ] || return "$rc"

  sha=$(printf '%s' "$body" | jq -r '.object.sha')
  type=$(printf '%s' "$body" | jq -r '.object.type')
  if [ "$type" = "tag" ]; then
    body=$(gh_get "repos/${repo}/git/tags/${sha}"); rc=$?
    [ "$rc" -eq 0 ] || return "$rc"
    sha=$(printf '%s' "$body" | jq -r '.object.sha')
  fi
  printf '%s' "$sha"
}

# Confirm the referenced action or reusable workflow exists at a commit.
# Prints the path it looked for. Same exit convention as gh_get.
check_path() {
  local repo="$1" subpath="$2" sha="$3" candidates=() c rc last=1
  # last: 1 = definitively absent, 2 = a request could not be completed
  if [ -n "$subpath" ]; then
    case "$subpath" in
      *.yml|*.yaml) candidates=("${subpath#/}") ;;
      *) candidates=("${subpath#/}/action.yml" "${subpath#/}/action.yaml") ;;
    esac
  else
    candidates=("action.yml" "action.yaml")
  fi
  for c in "${candidates[@]}"; do
    gh_get "repos/${repo}/contents/${c}?ref=${sha}" >/dev/null; rc=$?
    if [ "$rc" -eq 0 ]; then
      return 0
    fi
    # "Could not ask" outranks "not there": the last candidate is a 404 for
    # nearly every real action, and it must not mask an earlier transient
    # failure into a confident claim of absence.
    if [ "$rc" -eq 2 ] || [ "$last" -ne 2 ]; then
      last=$rc
    fi
  done
  printf '%s' "${candidates[0]}"
  return "$last"
}

# Fingerprint the content a reference resolves to at a commit. An empty path is
# the repository root, which is what a reference with no subpath names.
#
# Read over the API rather than from git: the audit checks out with
# `fetch-depth: 1` and `persist-credentials: false`, so the pinned commit's
# tree is not present locally and git has no credentials to fetch it, while the
# API needs only the token this script already uses. Directory entries carry
# their tree SHA, which is derived from everything beneath them, so a listing of
# names and SHAs registers a change to any file the action ships — not just its
# manifest. Same exit convention as gh_get.
content_fingerprint() {
  local repo="$1" path="$2" ref="$3" body rc query=""
  if [ -n "$ref" ]; then
    query="?ref=${ref}"
  fi
  body=$(gh_get "repos/${repo}/contents/${path}${query}"); rc=$?
  [ "$rc" -eq 0 ] || return "$rc"
  printf '%s' "$body" | jq -Sc '
    if type == "array" then [ .[] | {name, sha, type} ] | sort_by(.name)
    else [ {name, sha, type} ] end' || return 2
}

# Two SHAs match if one is a prefix of the other, since a pin may be abbreviated.
sha_matches() {
  # Git accepts either case in a written ref; the API always answers lowercase.
  local pinned="${1,,}" actual="${2,,}"
  [ "${actual#"$pinned"}" != "$actual" ] || [ "${pinned#"$actual"}" != "$pinned" ]
}

# ---------------------------------------------------------------------------
# Checks A and C, over each distinct reference.
#
# Fields are separated by US (0x1f) rather than tab: tab is an IFS whitespace
# character, so bash would collapse the empty `tag` field of an uncommented pin
# and silently shift every field after it.
# ---------------------------------------------------------------------------
while IFS=$'\x1f' read -r repo subpath ref tag class ref_kind files; do
  # A self-repository reference resolves to the commit already running, so
  # there is no upstream to re-resolve, no tag that can be re-pointed and no
  # content that can drift from the tree it was read out of. Every check below
  # is answered by construction. It is counted rather than skipped so the
  # totals still describe the whole file: an unverifiable reference and an
  # unverified one must not report identically.
  if [ "$ref_kind" = "self" ]; then
    CHECKED=$((CHECKED + 1))
    continue
  fi

  [ -n "$repo" ] || continue
  CHECKED=$((CHECKED + 1))
  short="${ref:0:9}"
  display="${repo}${subpath}"

  if [ "$ref_kind" != "sha" ]; then
    emit critical "- **\`${display}\` is not pinned to a commit** — it references \`${ref}\`, a mutable ref whose contents can be replaced upstream at any time with no change here (in ${files})"
    continue
  fi

  # --- Check C ------------------------------------------------------------
  missing=$(check_path "$repo" "$subpath" "$ref"); path_rc=$?
  if [ "$path_rc" -ne 0 ]; then
    if [ "$path_rc" -eq 2 ]; then
      emit warning "- \`${display}@${short}\` could not be checked for existence upstream (in ${files})"
    else
      emit critical "- **\`${display}\` does not exist at the pinned commit \`${short}\`.** Expected \`${missing}\` in \`${repo}\`. The reference resolves to a real commit but the action is not in it, so every run using this pin fails (in ${files})"
    fi
  fi

  # --- Check D ------------------------------------------------------------
  if [ -n "$SELF_REPO" ] && [ "${repo,,}" = "${SELF_REPO,,}" ]; then
    self_path="${subpath#/}"
    self_display="${self_path:-.}"
    pinned_fp=$(content_fingerprint "$repo" "$self_path" "$ref"); pin_rc=$?
    head_fp=$(content_fingerprint "$repo" "$self_path" "$SELF_HEAD"); head_rc=$?

    # Absence at the pinned commit is Check C's finding only where Check C made
    # it. Where Check C passed, the same absence means the comparison did not
    # happen, and an unmade comparison is a warning rather than a silent pass.
    if [ "$pin_rc" -eq 2 ] || [ "$head_rc" -eq 2 ] ||
      { [ "$pin_rc" -eq 1 ] && [ "$path_rc" -eq 0 ]; }; then
      emit warning "- \`${display}@${short}\` is a self-reference whose content could not be compared against the current tree (in ${files})"
    elif [ "$pin_rc" -eq 1 ]; then
      : # absent at the pinned commit — Check C's finding, reported there
    elif [ "$head_rc" -eq 1 ]; then
      emit warning "- \`${display}@${short}\` is a self-reference to \`${self_display}\`, which no longer exists in this repository — the pin is the only copy left (in ${files})"
    elif [ "$pinned_fp" != "$head_fp" ]; then
      emit warning "- \`${display}@${short}\` is a self-reference running an older copy of \`${self_display}\` — its content differs from the current tree, so this workflow does not run what is checked in beside it (in ${files})"
    fi
  fi

  # --- Check A ------------------------------------------------------------
  case "$class" in
    none)
      emit warning "- \`${display}@${short}\` has no version comment — reference cannot be integrity-checked (in ${files})"
      continue
      ;;
    unknown)
      emit warning "- \`${display}@${short}\` has the comment \`${tag}\`, which names neither a version nor a resolvable ref — reference cannot be integrity-checked (in ${files})"
      continue
      ;;
    branch)
      # Confirm the comment actually names a branch. The compare endpoint
      # resolves tags too, so a tag name would otherwise be checked as though
      # it were a branch and quietly pass.
      gh_get "repos/${repo}/branches/${tag}" >/dev/null; br_rc=$?
      if [ "$br_rc" -eq 1 ]; then
        emit critical "- **\`${display}\` comments \`${tag}\`, which is not a branch in \`${repo}\`.** A branch-tracking reference must name a real branch; nothing here can be resolved (in ${files})"
        continue
      fi
      if [ "$br_rc" -eq 2 ]; then
        emit warning "- \`${display}@${short}\` claims branch \`${tag}\`, which could not be looked up upstream (in ${files})"
        continue
      fi

      # behind/identical => the commit is on the branch; anything else => it is not.
      rel_body=$(gh_get "repos/${repo}/compare/${tag}...${ref}"); rel_rc=$?
      if [ "$rel_rc" -ne 0 ]; then
        emit warning "- \`${display}@${short}\` claims branch \`${tag}\`, which could not be compared upstream (in ${files})"
        continue
      fi
      rel=$(printf '%s' "$rel_body" | jq -r '.status')
      case "$rel" in
        behind|identical)
          emit info "- \`${display}@${short}\` tracks branch \`${tag}\` — on the branch, moves by design (in ${files})"
          ;;
        *)
          emit critical "- **\`${display}\` pins \`${short}\`, which is not on branch \`${tag}\`** (compare status: ${rel}). The commit resolves but is not reachable from the branch it claims to track, so it will break once that line is garbage collected (in ${files})"
          ;;
      esac
      continue
      ;;
  esac

  actual=$(resolve_tag "$repo" "$tag"); rc=$?

  if [ "$rc" -eq 2 ]; then
    emit warning "- \`${display}@${short}\` could not be resolved against tag \`${tag}\` upstream (in ${files})"
    continue
  fi

  if [ "$rc" -eq 1 ]; then
    if [ "$class" = "immutable" ]; then
      emit critical "- **\`${repo}\` tag \`${tag}\` does not exist upstream.** Pinned SHA \`${short}\`. An immutable release tag was deleted or renamed — treat as a potential compromise until confirmed otherwise (in ${files})"
    else
      emit info "- \`${repo}\` tag \`${tag}\` not found upstream (floating tag, may have been retired) (in ${files})"
    fi
    continue
  fi

  if sha_matches "$ref" "$actual"; then
    continue
  fi

  if [ "$class" = "immutable" ]; then
    emit critical "- **\`${repo}\` tag \`${tag}\` was re-pointed upstream.** Pinned \`${short}\`, tag resolves to \`${actual:0:9}\`. Immutable tags must never move — this is the signature of a force-push supply-chain attack (in ${files})"
  else
    emit info "- \`${repo}@${tag}\` drifted: pinned \`${short}\`, tag \`${actual:0:9}\` — expected for a floating tag (in ${files})"
  fi
done < <(
  echo "$PINS" | jq -r '
    group_by(.owner + "/" + .repo + .subpath + "@" + .ref + "#" + .tag)[]
    | [ (.[0].owner + "/" + .[0].repo),
        .[0].subpath,
        .[0].ref,
        .[0].tag,
        .[0].class,
        .[0].ref_kind,
        ([.[].file] | unique | map("`" + . + "`") | join(", "))
      ] | join("\u001f")'
)

# ---------------------------------------------------------------------------
# Check B - advisory lookup, once per distinct repository and version.
#
# An exact version is queried as OWNER/REPO@VERSION, so a pin on a patched
# release is not reported against an advisory it is not subject to. Without a
# usable version only the repository-wide query is available, and its results
# are warnings rather than criticals because they may describe a release this
# reference is not on.
# ---------------------------------------------------------------------------
while IFS=$'\x1f' read -r repo tag class; do
  [ -n "$repo" ] || continue

  if [ "$class" = "immutable" ]; then
    query="advisories?ecosystem=actions&per_page=100&affects=${repo}@${tag}"
    severity="critical"
  else
    query="advisories?ecosystem=actions&per_page=100&affects=${repo}"
    severity="warning"
  fi

  body=$(gh_get "$query"); rc=$?
  if [ "$rc" -eq 2 ]; then
    emit warning "- the advisory database could not be queried for \`${repo}\`"
    continue
  fi
  [ "$rc" -eq 0 ] || continue

  while IFS=$'\x1f' read -r ghsa sev summary; do
    [ -n "$ghsa" ] || continue
    if [ "$severity" = "critical" ]; then
      emit critical "- **Advisory affecting \`${repo}\` at \`${tag}\`** — [${ghsa}](https://github.com/advisories/${ghsa}) (${sev}): ${summary}"
    else
      emit warning "- Advisory published for \`${repo}\` — [${ghsa}](https://github.com/advisories/${ghsa}) (${sev}): ${summary}. This reference's version could not be matched against the advisory's range; confirm by hand."
    fi
  done < <(printf '%s' "$body" | jq -r '.[] | [.ghsa_id, .severity, (.summary | gsub("[\r\n]+"; " "))] | join("\u001f")')
done < <(
  echo "$PINS" | jq -r '
    [ .[] | select(.ref_kind == "sha")
      | { repo: (.owner + "/" + .repo), tag: .tag, class: .class } ]
    | unique | .[] | [.repo, .tag, .class] | join("\u001f")'
)

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
TOTAL=$(echo "$PINS" | jq 'length')

# Verifying nothing is not the same as verifying successfully. The scan fails
# loudly on error, so an empty set means the repository genuinely has no
# references — worth saying out loud rather than reporting a clean pass.
if [ "$TOTAL" -eq 0 ]; then
  emit warning "- no \`uses:\` references were found to verify"
fi

if [ "$CRITICAL" -gt 0 ]; then
  STATUS="critical"
elif [ "$WARNING" -gt 0 ]; then
  STATUS="warning"
else
  STATUS="ok"
fi

{
  if [ "$CRITICAL" -gt 0 ]; then
    printf '### Critical\n\n%s\n' "$CRIT_LINES"
  fi
  if [ "$WARNING" -gt 0 ]; then
    printf '### Warnings\n\n%s\n' "$WARN_LINES"
  fi
  if [ "$INFO" -gt 0 ]; then
    printf '<details><summary>Informational (%s) — expected drift on floating tags and branch pins</summary>\n\n%s</details>\n\n' "$INFO" "$INFO_LINES"
  fi
  if [ "$CRITICAL" -eq 0 ] && [ "$WARNING" -eq 0 ]; then
    printf 'No integrity or advisory findings.\n\n'
  fi
  # shellcheck disable=SC2016  # the backticks are literal markdown, not a subshell
  printf '_Verified %s distinct references across %s `uses:` lines._\n' "$CHECKED" "$TOTAL"
} > "${REPORT_FILE}"

cat "${REPORT_FILE}"

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  # Randomised delimiter: the report embeds upstream advisory text, and a fixed
  # sentinel would let that content close the block early and set later keys.
  DELIM="VERIFY_PINS_EOF_${RANDOM}${RANDOM}"
  {
    echo "status=${STATUS}"
    echo "critical=${CRITICAL}"
    echo "warning=${WARNING}"
    echo "info=${INFO}"
    echo "checked=${CHECKED}"
    echo "total=${TOTAL}"
    echo "report<<${DELIM}"
    cat "${REPORT_FILE}"
    echo "${DELIM}"
  } >> "$GITHUB_OUTPUT"
fi
