# shellcheck shell=bash
#
# Shared discovery and parsing for `uses:` references.
#
# This is the single definition of "what counts as an action reference and where
# do we look for them". Both the updater (which rewrites pins) and the verifier
# (which checks them) source this file, so the verifier cannot silently fall
# behind the set of files the updater edits.
#
# Two discovery modes emit the same JSON, so callers can work against a local
# checkout or against an arbitrary remote ref without one:
#
#   pins_scan_local                 - walk the working directory
#   pins_scan_remote REPO REF       - walk a ref via the GitHub API, no checkout
#
# Both return non-zero if discovery could not be completed. A scan that failed
# must never be mistaken for a scan that found nothing: callers gate merges on
# these results, and an empty list reads as "all clear".
#
# Output is a JSON array of objects:
#   { file, owner, repo, subpath, ref, tag, class, ref_kind }
#
# `ref_kind` is `sha` for a SHA-pinned reference and `unpinned` for anything
# else (`@v4`, `@main`). Unpinned references are reported rather than skipped:
# a pin that disappears from the scan is indistinguishable from a repository
# with nothing to check, which is how a PR that removes pinning would otherwise
# pass verification.
#
# `class` describes the version comment and drives severity:
#   immutable  exact semver tag (v7.0.0). Must never move.
#   mutable    floating major/minor tag (v2, v2.9). Moves by design.
#   branch     branch-tracking pin (main). Moves by design.
#   unknown    a comment that is neither, so nothing can be resolved from it.
#   none       no comment at all.

# Classify a version comment. See the `class` notes above.
pins_classify() {
  local tag="$1"
  if [ -z "$tag" ]; then
    echo "none"
  elif [[ "$tag" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$ ]]; then
    echo "immutable"
  elif [[ "$tag" =~ ^v?[0-9]+(\.[0-9]+)?$ ]]; then
    echo "mutable"
  elif [[ "$tag" =~ ^[A-Za-z0-9._/-]+$ ]]; then
    # Plausible git ref name. Anything with spaces or punctuation is a note to
    # a human, not something that can be resolved, and must not be passed to
    # the API as though it were a branch.
    echo "branch"
  else
    echo "unknown"
  fi
}

# Every file that can carry a `uses:` line: GitHub and Forgejo workflow
# definitions, plus composite action manifests (nested arbitrarily deep under
# .github/actions, so use find).
pins_discover_local() {
  find .github/workflows .forgejo/workflows -maxdepth 1 -type f \
    \( -name '*.yml' -o -name '*.yaml' \) 2>/dev/null
  find .github/actions -type f \
    \( -name 'action.yml' -o -name 'action.yaml' \) 2>/dev/null
}

# Remote equivalent of pins_discover_local, using the git tree API so a caller
# can scan a ref it has not checked out (e.g. each open PR's head). Returns
# non-zero if the tree could not be read.
pins_discover_remote() {
  local repo="$1" ref="$2" body tree
  body=$(gh api "repos/${repo}/git/trees/${ref}?recursive=1") || return 1

  # The tree API caps its response and flags the cut with `truncated`. A
  # shortened listing would drop files from a scan that reports success.
  if [ "$(printf '%s' "$body" | jq -r '.truncated')" = "true" ]; then
    return 1
  fi
  tree=$(printf '%s' "$body" | jq -r '.tree[] | select(.type == "blob") | .path')

  printf '%s\n' "$tree" | while IFS= read -r path; do
    case "$path" in
      .github/workflows/*/*|.forgejo/workflows/*/*) continue ;;
      .github/workflows/*.yml|.github/workflows/*.yaml) echo "$path" ;;
      .forgejo/workflows/*.yml|.forgejo/workflows/*.yaml) echo "$path" ;;
      .github/actions/*/action.yml|.github/actions/*/action.yaml) echo "$path" ;;
    esac
  done
}

# Trim leading and trailing whitespace without invoking xargs, which treats its
# input as shell-quoted words and mangles comments containing quotes.
pins_trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

# Parse `uses:` lines out of one file's content.
#
# Recognised shapes, with the value optionally quoted:
#   OWNER/REPO@REF # vX.Y.Z              most third-party actions
#   OWNER/REPO/PATH@REF # vX.Y.Z         composite actions or reusable workflows
#                                        living inside a shared repo
# The second-segment match excludes / and @ to keep the repo name exactly one
# path segment; SUBPATH then captures the rest up to the @.
#
# Lines whose first non-space character is `#` are skipped: commented-out steps
# do not run, so verifying them produces findings for code that cannot execute.
# The match is anchored to a step key, so a `uses:`-shaped string inside a `run:`
# body or an input value does not register as a reference.
pins_parse_content() {
  local file="$1" content="$2"
  local line stripped owner repo subpath ref tag class ref_kind

  while IFS= read -r line; do
    stripped=$(pins_trim "$line")
    case "$stripped" in
      '#'*) continue ;;
    esac

    if [[ "$line" =~ ^[[:space:]]*-?[[:space:]]*uses:[[:space:]]*[\"\']?([^/[:space:]\"\']+)/([^/@[:space:]\"\']+)(/[^@[:space:]\"\']*)?@([^[:space:]\"\'#]+)[\"\']?[[:space:]]*(#[[:space:]]*(.*))?$ ]]; then
      owner="${BASH_REMATCH[1]}"
      repo="${BASH_REMATCH[2]}"
      subpath="${BASH_REMATCH[3]}"
      ref="${BASH_REMATCH[4]}"
      tag=$(pins_trim "${BASH_REMATCH[6]}")
      class=$(pins_classify "$tag")

      if [[ "$ref" =~ ^[a-fA-F0-9]{7,40}$ ]]; then
        ref_kind="sha"
      else
        ref_kind="unpinned"
      fi

      jq -n \
        --arg file "$file" \
        --arg owner "$owner" \
        --arg repo "$repo" \
        --arg subpath "$subpath" \
        --arg ref "$ref" \
        --arg tag "$tag" \
        --arg class "$class" \
        --arg ref_kind "$ref_kind" \
        '{file: $file, owner: $owner, repo: $repo, subpath: $subpath, ref: $ref, tag: $tag, class: $class, ref_kind: $ref_kind}'
    fi
  done <<< "$content"
}

# Scan the working directory. Emits a JSON array.
pins_scan_local() {
  local file records=""
  while IFS= read -r file; do
    [ -f "$file" ] || continue
    records+=$(pins_parse_content "$file" "$(cat "$file")")
    records+=$'\n'
  done < <(pins_discover_local)

  printf '%s' "$records" | jq -s '.'
}

# Scan a remote ref without checking it out. Emits a JSON array.
#
# Any API failure aborts the whole scan rather than dropping the file it could
# not read: a partial scan reported as complete is a silent verification gap.
pins_scan_remote() {
  local repo="$1" ref="$2" file content records="" files
  files=$(pins_discover_remote "$repo" "$ref") || return 1

  while IFS= read -r file; do
    [ -n "$file" ] || continue
    content=$(gh api "repos/${repo}/contents/${file}?ref=${ref}" --jq '.content') || return 1
    content=$(printf '%s' "$content" | base64 -d) || return 1
    records+=$(pins_parse_content "$file" "$content")
    records+=$'\n'
  done <<< "$files"

  printf '%s' "$records" | jq -s '.'
}
