"""Offline integration tests: local Git fixtures and an allowlisted, stateful gh stub."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/scripts/release.py"
REAL_GIT = shutil.which("git")
NOTES = "Reviewed release notes.\n"

# No real gh, model CLI, shell, curl, SSH or network-capable Git command is on
# the child's PATH. Python sockets are denied as well, including in the gh stub.
GH_STUB = r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
assert args[:5] == ["api", "--hostname", "github.com", "--include", "--method"]
method, path = args[5:7]
assert path.startswith("repos/arcuru/actions/")
path = path.removeprefix("repos/arcuru/actions/")
assert method in ("GET", "POST")
assert args[7:] == (["--input", "-"] if method == "POST" else [])
payload = json.load(sys.stdin) if method == "POST" else None
state_path = Path(os.environ["STUB_STATE"])
state = json.loads(state_path.read_text())
state["calls"].append([method, path, payload])
status, data = 200, {}
if state.get("get_failure") and method == "GET":
    status = state["get_failure"]
elif method == "GET" and path.startswith("git/ref/tags/"):
    tag = path.removeprefix("git/ref/tags/")
    if tag not in state["tags"]:
        status = 404
    else:
        data = {"ref": "refs/tags/" + tag, "object": state["tags"][tag]}
elif method == "GET" and path.startswith("git/tags/"):
    data = {"object": state["objects"][path.removeprefix("git/tags/")]}
elif method == "GET" and path.startswith("releases/tags/"):
    tag = path.removeprefix("releases/tags/")
    if tag not in state["releases"]:
        status = 404
    else:
        data = state["releases"][tag]
elif method == "POST" and path == "git/refs":
    assert set(payload) == {"ref", "sha"}
    tag = payload["ref"].removeprefix("refs/tags/")
    assert re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag)
    assert re.fullmatch(r"[0-9a-f]{40}", payload["sha"])
    if tag in state["tags"]:
        status = 422
    elif state.get("tag_failure"):
        status = 503
    else:
        state["tags"][tag] = {"type": "commit", "sha": state.get("race_sha", payload["sha"])}
        status = 422 if state.get("race_tag") else 201
elif method == "POST" and path == "releases":
    assert set(payload) == {"tag_name", "target_commitish", "name", "body", "draft", "prerelease"}
    assert payload["tag_name"] in state["tags"]
    assert payload["draft"] is False and payload["prerelease"] is False
    if payload["tag_name"] in state["releases"]:
        status = 422
    elif state.get("release_failure"):
        status = 503
    else:
        state["releases"][payload["tag_name"]] = payload
        status = 422 if state.get("race_release") else 201
else:
    raise AssertionError("Forbidden remote operation: " + str(args))
state_path.write_text(json.dumps(state))
print(f"HTTP/2.0 {status} Stub\nContent-Type: application/json\n\n" + json.dumps(data))
sys.exit(0 if status < 400 else 1)
'''


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.state = self.root / "state.json"
        self.state.write_text(json.dumps({"calls": [], "tags": {}, "releases": {}, "objects": {}}))
        self.env = {
            "PATH": str(self.bin), "HOME": str(self.root),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GITHUB_EVENT_NAME": "push", "GITHUB_REPOSITORY": "arcuru/actions",
            "GITHUB_REPOSITORY_OWNER": "arcuru", "GITHUB_REF": "refs/heads/main",
            "VALIDATION_RESULT": "success", "STUB_STATE": str(self.state),
            "GITHUB_EVENT_PATH": str(self.root / "event.json"),
            "PYTHONPATH": str(self.bin), "PYTHONDONTWRITEBYTECODE": "1",
            "LC_ALL": "C.UTF-8",
        }
        (self.bin / "sitecustomize.py").write_text(
            'import socket\n'
            'def denied(*args, **kwargs):\n    raise RuntimeError("Offline test: network denied")\n'
            'socket.socket.connect = denied\nsocket.create_connection = denied\n'
        )
        self.executable("gh", "import re\n" + GH_STUB)
        self.executable("git", f'''import os, subprocess, sys
assert sys.argv[1] in ("ls-tree", "show", "rev-parse", "merge-base"), "Forbidden Git command"
sys.exit(subprocess.run([{REAL_GIT!r}, *sys.argv[1:]], env=os.environ).returncode)
''')
        self.git("init", "-qb", "topic")
        (self.repo / "README").write_text("Baseline\n")
        self.base = self.commit()
        self.request()
        self.sha = self.commit()
        self.event(self.base, self.sha)

    def executable(self, name, body):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o755)

    def git(self, *args):
        return subprocess.check_output([REAL_GIT, *args], cwd=self.repo,
                                       env=self.env, text=True).strip()

    def commit(self):
        self.git("add", "--all")  # Only the disposable local fixture, never a real worktree.
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD")

    def request(self, version="0.5.0", notes=NOTES):
        (self.repo / "VERSION").write_text(version + "\n")
        (self.repo / "releases").mkdir(exist_ok=True)
        (self.repo / f"releases/v{version}.md").write_text(notes)

    def event(self, before, after):
        self.env["GITHUB_SHA"] = after
        self.payload = {"before": before, "after": after, "ref": "refs/heads/main",
                        "deleted": False, "forced": False,
                        "repository": {"full_name": "arcuru/actions"}}
        self.save_event()

    def save_event(self):
        Path(self.env["GITHUB_EVENT_PATH"]).write_text(json.dumps(self.payload))

    def data(self, **updates):
        data = json.loads(self.state.read_text())
        if updates:
            data.update(updates)
            self.state.write_text(json.dumps(data))
        return data

    def run_script(self, mode="publish", success=True, message=None):
        result = subprocess.run([sys.executable, str(SCRIPT), mode], cwd=self.repo,
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        if message:
            self.assertIn(message, result.stdout + result.stderr)
        return result

    def no_writes(self):
        self.assertFalse([call for call in self.data()["calls"] if call[0] != "GET"])

    def test_initial_release_exact_event_sha_and_notes(self):
        self.run_script("validate", message="version bump: True")
        self.assertEqual(self.data()["calls"], [])
        self.run_script(message=f"v0.5.0 at {self.sha}")
        data = self.data()
        self.assertEqual(data["tags"]["v0.5.0"], {"type": "commit", "sha": self.sha})
        self.assertEqual(data["releases"]["v0.5.0"], {
            "tag_name": "v0.5.0", "target_commitish": self.sha, "name": "v0.5.0",
            "body": NOTES, "draft": False, "prerelease": False})
        self.assertEqual([call[1] for call in data["calls"] if call[0] == "POST"],
                         ["git/refs", "releases"])

    def test_subsequent_bump(self):
        self.request("0.6.0", "Next reviewed notes\n")
        sha = self.commit()
        self.event(self.sha, sha)
        self.run_script()
        self.assertEqual(self.data()["tags"]["v0.6.0"]["sha"], sha)

    def test_unrelated_push_skips_even_if_earlier_release_needs_retry(self):
        (self.repo / "README").write_text("Unrelated\n")
        after = self.commit()
        self.event(self.sha, after)
        self.run_script(message="No version bump")
        self.assertEqual(self.data()["calls"], [])

    def test_repeat_is_read_only(self):
        self.run_script()
        self.data(calls=[])
        self.run_script()
        self.no_writes()

    def test_partial_tag_then_release_failure_recovers(self):
        self.data(release_failure=True)
        self.run_script(success=False, message="Release creation failed")
        self.assertIn("v0.5.0", self.data()["tags"])
        self.assertEqual(self.data()["releases"], {})
        self.data(release_failure=False, calls=[])
        self.run_script()
        self.assertEqual([call[1] for call in self.data()["calls"] if call[0] == "POST"], ["releases"])

    def test_racing_identical_creations_are_verified(self):
        self.data(race_tag=True, race_release=True)
        self.run_script()
        self.assertEqual(self.data()["releases"]["v0.5.0"]["target_commitish"], self.sha)

    def test_racing_different_tag_fails_without_release(self):
        self.data(race_tag=True, race_sha=self.base)
        self.run_script(success=False, message="Tag creation failed/conflicted")
        self.assertEqual(self.data()["releases"], {})

    def test_tag_creation_failure_does_not_create_release(self):
        self.data(tag_failure=True)
        self.run_script(success=False, message="Tag creation failed")
        self.assertEqual(self.data()["releases"], {})

    def test_immutable_tag_collision(self):
        self.data(tags={"v0.5.0": {"type": "commit", "sha": self.base}})
        self.run_script(success=False, message="Immutable tag")
        self.no_writes()

    def test_annotated_tag_recovery(self):
        obj = "a" * 40
        self.data(tags={"v0.5.0": {"type": "tag", "sha": obj}},
                  objects={obj: {"type": "commit", "sha": self.sha}})
        self.run_script()
        self.assertEqual(self.data()["tags"]["v0.5.0"]["sha"], obj)

    def test_conflicting_release_metadata_is_never_overwritten(self):
        self.run_script()
        original = self.data()["releases"]["v0.5.0"]
        for key, value in {"target_commitish": self.base, "body": "Foreign notes",
                           "name": "Other", "draft": True, "prerelease": True,
                           "tag_name": "v0.1.0"}.items():
            with self.subTest(key=key):
                self.data(calls=[], releases={"v0.5.0": {**original, key: value}})
                self.run_script(success=False, message="Existing release conflicts")
                self.no_writes()

    def test_orphan_release_is_not_repaired(self):
        self.data(releases={"v0.5.0": {}})
        self.run_script(success=False, message="Release exists without")
        self.no_writes()

    def test_read_failure_is_not_absence(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                self.data(get_failure=status, calls=[])
                self.run_script(success=False, message=f"HTTP {status}")
                self.no_writes()

    def test_transport_failure_fails_closed(self):
        self.executable("gh", 'import sys\nprint("Network unavailable", file=sys.stderr)\nsys.exit(1)\n')
        self.run_script(success=False, message="no HTTP response")
        self.no_writes()

    def test_pr_and_fork_publish_denied(self):
        for key, value in {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_REPOSITORY": "arcuru-bot/actions",
                           "GITHUB_REPOSITORY_OWNER": "arcuru-bot", "GITHUB_REF": "refs/heads/other"}.items():
            with self.subTest(key=key):
                original = self.env[key]
                self.env[key] = value
                self.run_script(success=False, message="Publishing requires")
                self.env[key] = original
                self.assertEqual(self.data()["calls"], [])

    def test_failed_or_missing_gate_denies_all_api_calls(self):
        for result in ("failure", "cancelled", "skipped", ""):
            with self.subTest(result=result):
                self.env["VALIDATION_RESULT"] = result
                self.run_script(success=False, message="Validation gate must succeed")
                self.assertEqual(self.data()["calls"], [])

    def test_wrong_event_repository_denied(self):
        self.payload["repository"]["full_name"] = "other/actions"
        self.save_event()
        self.run_script(success=False, message="Unexpected event repository")
        self.assertEqual(self.data()["calls"], [])

    def test_invalid_versions(self):
        for value in ("", " ", "v0.5.0", "01.2.3", "1.2", "1.2.3-rc.1", "1.2.3+build",
                      "1.2.3\n\n", "../../main", "1.2.3; echo unsafe"):
            with self.subTest(value=value):
                (self.repo / "VERSION").write_text(value)
                after = self.commit()
                self.event(self.base, after)
                self.run_script(success=False, message="VERSION must contain")
                self.assertEqual(self.data()["calls"], [])

    def test_missing_and_blank_inputs(self):
        for path in ("VERSION", "releases/v0.5.0.md"):
            with self.subTest(path=path):
                file = self.repo / path
                contents = file.read_text()
                file.unlink()
                after = self.commit()
                self.event(self.base, after)
                self.run_script(success=False, message="must be a regular file")
                file.write_text(contents)
        (self.repo / "releases/v0.5.0.md").write_text(" \n\t\n")
        after = self.commit()
        self.event(self.base, after)
        self.run_script(success=False, message="must not be blank")
        self.assertEqual(self.data()["calls"], [])

    def test_symlink_notes_denied(self):
        notes = self.repo / "releases/v0.5.0.md"
        notes.unlink()
        notes.symlink_to("../README")
        after = self.commit()
        self.event(self.base, after)
        self.run_script(success=False, message="must be a regular file")
        self.assertEqual(self.data()["calls"], [])

    def test_downgrade_and_same_version_notes_edit_denied(self):
        self.request("0.4.0")
        after = self.commit()
        self.event(self.sha, after)
        self.run_script(success=False, message="version must increase")
        self.request(notes="Edited published notes")
        after = self.commit()
        self.event(self.sha, after)
        self.run_script(success=False, message="notes cannot change")
        self.assertEqual(self.data()["calls"], [])

    def test_multi_commit_merge_releases_tip_not_bump_commit_or_later_main(self):
        bump = self.sha
        # A second PR commit followed by the actual main merge commit.
        (self.repo / "README").write_text("Second PR commit\n")
        self.commit()
        self.git("checkout", "-qb", "main", self.base)
        self.git("-c", "commit.gpgsign=false", "merge", "--no-ff", "-m", "Merge release PR", "topic")
        merged = self.git("rev-parse", "HEAD")
        self.assertNotEqual(merged, bump)
        self.event(self.base, merged)
        self.git("branch", "later-main")
        self.git("checkout", "later-main")
        (self.repo / "README").write_text("Later main\n")
        self.commit()
        self.git("checkout", "-q", merged)
        self.run_script()
        self.assertEqual(self.data()["tags"]["v0.5.0"]["sha"], merged)

    def test_stale_checkout_and_tampered_event_denied(self):
        self.env["GITHUB_SHA"] = self.base
        self.run_script(success=False, message="Checkout must equal")
        self.env["GITHUB_SHA"] = "main"
        self.run_script(success=False, message="Invalid event SHA")
        self.env["GITHUB_SHA"] = self.sha
        for key, value in {"after": self.base, "ref": "refs/heads/other", "forced": True,
                           "deleted": True, "before": "0" * 40}.items():
            with self.subTest(key=key):
                original = self.payload[key]
                self.payload[key] = value
                self.save_event()
                self.run_script(success=False)
                self.payload[key] = original
        self.assertEqual(self.data()["calls"], [])

    def test_pr_validation_is_read_only(self):
        self.env["GITHUB_EVENT_NAME"] = "pull_request"
        self.payload["pull_request"] = {"base": {"sha": self.base}}
        self.save_event()
        self.run_script("validate", message="Validated v0.5.0")
        self.assertEqual(self.data()["calls"], [])

    def test_offline_boundary(self):
        for name in ("curl", "ssh", "claude", "pi", "ava-model-run"):
            self.assertIsNone(shutil.which(name, path=self.env["PATH"]))
        result = subprocess.run([sys.executable, "-c", "import socket; socket.create_connection(('127.0.0.1', 443))"],
                                env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("network denied", result.stderr)
        result = subprocess.run([str(self.bin / "git"), "push"], env=self.env,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Forbidden Git command", result.stderr)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        self.publish = self.workflow.split("\n  publish:\n", 1)[1]

    def expression(self, expression, **context):
        # Evaluate the actual small workflow boolean expressions, not a second
        # hand-copied eligibility implementation. No builtins or external input.
        expression = " ".join(expression.split()).replace("&&", " and ").replace("||", " or ")
        return eval(expression, {"__builtins__": {}}, {"github": SimpleNamespace(**context)})

    def test_publish_fence_and_real_gate_dependency(self):
        self.assertIn("    needs: actionlint\n", self.publish)
        self.assertIn("VALIDATION_RESULT: ${{ needs.actionlint.result }}", self.publish)
        condition = self.publish.split("    if: >-\n", 1)[1].split("    runs-on:", 1)[0]
        context = dict(repository_owner="arcuru", repository="arcuru/actions",
                       event_name="push", ref="refs/heads/main")
        self.assertTrue(self.expression(condition, **context))
        for key, value in dict(repository_owner="arcuru-bot", repository="arcuru-bot/actions",
                               event_name="pull_request", ref="refs/heads/other").items():
            self.assertFalse(self.expression(condition, **{**context, key: value}))
        self.assertNotIn("always()", self.publish)
        validation = self.workflow.split("\n  publish:\n")[0]
        self.assertIn("run: python3 .github/scripts/release.py validate", validation)
        self.assertIn("python3 -m unittest discover", validation)
        self.assertIn("nix run nixpkgs#actionlint", validation)
        self.assertNotIn("continue-on-error", self.workflow)

    def test_main_runs_have_distinct_groups_and_cannot_replace_pending_release(self):
        group = re.search(r"  group: ci-\$\{\{ (.+) \}\}", self.workflow)[1]
        cancel = re.search(r"  cancel-in-progress: \$\{\{ (.+) \}\}", self.workflow)[1]
        main = dict(event_name="push", ref="refs/heads/main", run_id=1)
        self.assertNotEqual(self.expression(group, **main), self.expression(group, **{**main, "run_id": 2}))
        self.assertFalse(self.expression(cancel, **main))
        pr = dict(event_name="pull_request", ref="refs/pull/1/merge", run_id=1)
        self.assertEqual(self.expression(group, **pr), self.expression(group, **{**pr, "run_id": 2}))
        self.assertTrue(self.expression(cancel, **pr))

    def test_permissions_checkout_and_shell_boundaries(self):
        self.assertIn("permissions:\n  contents: read\n", self.workflow)
        self.assertEqual(self.workflow.count("contents: write"), 1)
        self.assertIn("ref: ${{ github.sha }}", self.publish)
        self.assertIn("GH_TOKEN: ${{ github.token }}", self.publish)
        self.assertEqual(self.workflow.count("persist-credentials: false"), 2)
        self.assertEqual(self.workflow.count("fetch-depth: 0"), 2)
        self.assertNotIn("workflow_run", self.workflow)
        self.assertNotIn("pull_request_target", self.workflow)
        self.assertNotIn("PAT_TOKEN", self.workflow)
        for block in re.findall(r"run: (.*?)(?=\n\s+- name:|\n\s+\w+:|\Z)", self.workflow, re.S):
            self.assertNotIn("${{", block)


if __name__ == "__main__":
    unittest.main()
