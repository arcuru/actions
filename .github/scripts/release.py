#!/usr/bin/env python3
"""Validate release PRs; publish only the validated trusted-main push event."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys

REPOSITORY = "arcuru/actions"
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
SHA = re.compile(r"[0-9a-f]{40}")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def blob(ref, path, optional=False):
    entry = git("ls-tree", ref, "--", path)
    if optional and not entry:
        return None
    require(entry.startswith("100644 blob "), f"{ref}:{path} must be a regular file")
    return subprocess.check_output(["git", "show", f"{ref}:{path}"], text=True)


def manifest(ref, optional=False):
    version = blob(ref, "VERSION", optional)
    if version is None:
        return None
    # One stable SemVer, with at most a final newline; never a ref/path expression.
    version = version.removesuffix("\n")
    require(VERSION.fullmatch(version), "VERSION must contain a stable X.Y.Z version")
    notes = blob(ref, f"releases/v{version}.md")
    require(notes.strip(), f"Release notes for v{version} must not be blank")
    return version, notes


def validate():
    sha = os.environ["GITHUB_SHA"]
    require(SHA.fullmatch(sha), "Invalid event SHA")
    require(git("rev-parse", "HEAD") == sha, "Checkout must equal the event SHA")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    kind = os.environ["GITHUB_EVENT_NAME"]
    if kind == "push":
        require(event.get("after") == sha, "Push after must equal the event SHA")
        require(event.get("ref") == "refs/heads/main", "Expected main push")
        require(event.get("deleted") is False and event.get("forced") is False,
                "Deleted/forced main pushes cannot release")
        before = event["before"]
    elif kind == "pull_request":
        before = event["pull_request"]["base"]["sha"]
    else:
        raise ValueError("Only pull_request and push validation are supported")
    require(SHA.fullmatch(before) and before != "0" * 40, "Invalid base SHA")
    require(subprocess.run(["git", "merge-base", "--is-ancestor", before, sha]).returncode == 0,
            "Base must be an ancestor of the event SHA (fetch full history)")
    current = manifest(sha)
    previous = manifest(before, optional=True)
    if previous:
        old_version, old_notes = previous
        if current[0] == old_version:
            require(current[1] == old_notes, "Released notes cannot change without a version bump")
            return sha, current, False
        require(tuple(map(int, current[0].split("."))) > tuple(map(int, old_version.split("."))),
                "Release version must increase")
    return sha, current, True


def api(method, path, payload=None):
    # gh is the sole network boundary. Explicit host/repository, no inferred CLI context.
    command = ["gh", "api", "--hostname", "github.com", "--include", "--method", method,
               f"repos/{REPOSITORY}/{path}"]
    if payload is not None:
        command += ["--input", "-"]
    result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                            text=True, capture_output=True)
    headers, separator, body = result.stdout.partition("\n\n")
    match = re.match(r"HTTP/\S+ (\d{3})\b", headers)
    require(separator and match, f"GitHub {method} {path}: no HTTP response: {result.stderr}")
    status = int(match[1])
    require(result.returncode == 0 or status >= 400, f"GitHub transport failure: {result.stderr}")
    return status, json.loads(body)


def get(path):
    status, data = api("GET", path)
    if status == 404:
        return None
    require(status == 200, f"GitHub GET {path} failed: HTTP {status}")
    return data


def tag_target(tag):
    ref = get(f"git/ref/tags/{tag}")
    if ref is None:
        return None
    require(ref.get("ref") == f"refs/tags/{tag}", "Unexpected tag reference")
    obj = ref["object"]
    # Existing annotated tags are immutable too; compare the peeled commit.
    for _ in range(10):
        require(SHA.fullmatch(obj["sha"]), "Invalid tag object SHA")
        if obj["type"] == "commit":
            return obj["sha"]
        require(obj["type"] == "tag", "Tag must resolve to a commit")
        data = get(f"git/tags/{obj['sha']}")
        require(data is not None, "Missing annotated tag object")
        obj = data["object"]
    raise ValueError("Annotated tag nesting is too deep")


def check_release(release, expected):
    require(all(release.get(key) == value for key, value in expected.items()),
            "Existing release conflicts with the approved SHA, notes or stable release metadata")


def publish():
    require(os.environ.get("GITHUB_REPOSITORY") == REPOSITORY
            and os.environ.get("GITHUB_REPOSITORY_OWNER") == "arcuru"
            and os.environ.get("GITHUB_EVENT_NAME") == "push"
            and os.environ.get("GITHUB_REF") == "refs/heads/main",
            "Publishing requires an arcuru/actions main push")
    require(os.environ.get("VALIDATION_RESULT") == "success", "Validation gate must succeed")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    require(event["repository"]["full_name"] == REPOSITORY, "Unexpected event repository")
    sha, (version, notes), changed = validate()
    if not changed:
        print("No version bump; nothing to publish")
        return
    tag = f"v{version}"
    expected = {"tag_name": tag, "target_commitish": sha, "name": tag, "body": notes,
                "draft": False, "prerelease": False}
    target = tag_target(tag)
    require(target in (None, sha), f"Immutable tag {tag} points to another commit")
    release = get(f"releases/tags/{tag}")
    if release is not None:
        require(target == sha, "Release exists without its expected tag")
        check_release(release, expected)
    if target is None:
        status, _ = api("POST", "git/refs", {"ref": f"refs/tags/{tag}", "sha": sha})
        # Creation is compare-and-set. A racing run or lost response is safe only
        # if the now-existing immutable tag proves the exact intended commit.
        require(tag_target(tag) == sha, f"Tag creation failed/conflicted: HTTP {status}")
    if release is None:
        # Delayed older runs must not displace a newer stable release as latest.
        status, _ = api("POST", "releases", {**expected, "make_latest": "legacy"})
        release = get(f"releases/tags/{tag}")
        require(release is not None, f"Release creation failed: HTTP {status}; rerun this CI run")
        check_release(release, expected)
    require(tag_target(tag) == sha, "Tag changed during publication")
    print(f"Published/verified {tag} at {sha}")


def main():
    require(len(sys.argv) == 2 and sys.argv[1] in ("validate", "publish"),
            "Usage: release.py validate|publish")
    if sys.argv[1] == "publish":
        publish()
    else:
        sha, (version, _), changed = validate()
        print(f"Validated v{version} at {sha}; version bump: {changed}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"Release error: {error}")
