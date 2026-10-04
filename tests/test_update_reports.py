# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6,<7"]
# ///
"""Run with `uv run tests/test_update_reports.py`; no network or git writes."""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
RUN_URL = "https://github.com/arcuru/example/actions/runs/123"
PAYLOAD = '$(touch INJECTED) `touch INJECTED` ${HOME}; "quotes" \\ backslash\nEOF\n'


def load_steps(path, job=None):
    document = yaml.safe_load((ROOT / path).read_text())
    return document["jobs"][job]["steps"] if job else document["runs"]["steps"]


class UpdateReports(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="update-reports-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runner = self.root / "runner temp"
        self.runner.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.output = self.root / "github-output"
        self.output.touch()
        self.context = {
            "runner.temp": str(self.runner),
            "github.server_url": "https://github.com",
            "github.repository": "arcuru/example",
            "github.run_id": "123",
            "inputs.pr-body": "",
            "inputs.pr-body-file": "",
            "inputs.branch": "deps/test-update-2026-10-03",
            "inputs.files": "Cargo.lock",
            "inputs.pr-title": "chore(deps): update dependencies",
            "inputs.update-comment": "Updated with latest results.",
            "inputs.github-token": "fake-token",
            "steps.hold.outputs.date": "2026-10-10",
            "steps.hold.outputs.days": "7",
        }
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(self.runner),
            "GITHUB_OUTPUT": str(self.output),
            "TEST_LOG": str(self.root / "update.log"),
            "TEST_METADATA": str(self.root / "metadata.json"),
            "TEST_DIFF": str(self.root / "diff.txt"),
            "TEST_GH_CALLS": str(self.root / "gh-calls.txt"),
            "TEST_GH_BODY": str(self.root / "posted-body.md"),
            "TEST_STATUS": "0",
            "TEST_EXISTING_PR": "",
        }
        self.write_stub("nix", '''
case "$*" in
  'develop --command true')
    printf 'NIX_BUILD_NOISE building dev shell\\n%.0s' {1..10000} >&2 ;;
  'flake metadata --json') cat "$TEST_METADATA" ;;
  'flake update'|'develop --command cargo update')
    cat "$TEST_LOG" >&2
    exit "$TEST_STATUS" ;;
  *) echo "unexpected nix invocation: $*" >&2; exit 99 ;;
esac
''')
        self.write_stub("git", '''
case "$*" in
  'diff --stat Cargo.lock') cat "$TEST_DIFF" ;;
  'diff --quiet -- Cargo.lock') exit 0 ;;
  *) echo "unexpected git invocation: $*" >&2; exit 99 ;;
esac
''')
        self.write_stub("gh", '''
printf '%s\\n' "$@" >> "$TEST_GH_CALLS"
case "$1 $2" in
  'pr list') printf '%s' "$TEST_EXISTING_PR" ;;
  'pr create'|'pr edit')
    BODY_FILE=""
    while (( $# )); do
      case "$1" in
        --body) echo 'PR body must use --body-file' >&2; exit 99 ;;
        --body-file) BODY_FILE="$2"; shift ;;
      esac
      shift
    done
    test -n "$BODY_FILE"
    cp "$BODY_FILE" "$TEST_GH_BODY"
    printf 'https://github.com/arcuru/example/pull/42\\n' ;;
  'pr comment') : ;;
  *) echo "unexpected gh invocation: $*" >&2; exit 99 ;;
esac
''')
        self.cargo = load_steps(".github/workflows/cargo-update.yml", "cargo-update")
        self.flake = load_steps(".github/workflows/flake-update.yml", "flake-update")
        self.composite = load_steps(".github/actions/commit-and-pr/action.yml")
        Path(self.env["TEST_DIFF"]).write_text(" Cargo.lock | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)\n")

    def write_stub(self, name, source):
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\nset -eo pipefail\n" + source)
        path.chmod(0o755)

    def step(self, steps, name):
        return next(step for step in steps if step["name"] == name)

    def render(self, value):
        return re.sub(r"\$\{\{\s*([^}]+?)\s*\}\}", lambda m: self.context[m[1]], value)

    def run_step(self, steps, name, overrides=None):
        step = self.step(steps, name)
        # Expressions are allowed in env, never in executable shell source.
        self.assertNotIn("${{", step["run"])
        env = {**self.env, **{k: self.render(v) for k, v in step.get("env", {}).items()}}
        env.update(overrides or {})
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
            cwd=self.root, env=env, capture_output=True, timeout=30,
        )

    def successful_step(self, steps, name):
        result = self.run_step(steps, name)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result

    def post_body(self, workflow):
        step = self.step(workflow, "Commit and create PR")
        self.assertNotIn("pr-body", step["with"])
        body_file = self.render(step["with"]["pr-body-file"])
        self.context["inputs.pr-body-file"] = body_file
        self.successful_step(self.composite, "Create or update PR")
        body = Path(self.env["TEST_GH_BODY"]).read_bytes()
        self.assertLess(len(body), 65536)
        text = body.decode("utf-8")
        self.assertIn("<!-- hold-until: 2026-10-10 -->", text)
        self.assertIn("(7-day waiting period)", text)
        self.assertFalse((self.root / "INJECTED").exists())
        return text

    def cargo_body(self, log):
        Path(self.env["TEST_LOG"]).write_text(log)
        warm = self.successful_step(self.cargo, "Warm the dev shell")
        self.assertGreater(len(warm.stderr), 200000)
        result = self.successful_step(self.cargo, "Run cargo update")
        self.assertEqual(result.stdout, log.encode())
        self.assertEqual(self.output.read_text(), "")
        self.successful_step(self.cargo, "Capture diff stats")
        self.successful_step(self.cargo, "Generate PR body")
        body = self.post_body(self.cargo)
        self.assertEqual((self.runner / "cargo-update.txt").read_text(), log)
        self.assertNotIn("NIX_BUILD_NOISE", body)
        self.assertIn("Cargo.lock | 2 +-", body)
        return body

    def test_cargo_small_report_is_literal(self):
        body = self.cargo_body("Updating dependencies\n" + PAYLOAD)
        self.assertIn(PAYLOAD, body)
        self.assertNotIn("truncated", body)
        calls = Path(self.env["TEST_GH_CALLS"]).read_text()
        self.assertIn("--head\ndeps/test-update-2026-10-03\n--base\nmain\n", calls)
        self.assertIn("--label\ndependencies\n--label\non-hold\n", calls)

    def test_cargo_output_cannot_execute_shell_commands(self):
        command = ': "$(touch INJECTED)" "`touch INJECTED`"; :\n'
        body = self.cargo_body(command)
        self.assertIn(command, body)

    def test_cargo_large_utf8_report_is_bounded_without_losing_full_log(self):
        body = self.cargo_body("Updating café → package\n" * 15000 + PAYLOAD)
        self.assertIn(PAYLOAD, body)
        self.assertIn("truncated", body)
        self.assertIn(RUN_URL, body)

    def test_cargo_large_single_line_report(self):
        body = self.cargo_body("é" * 150000 + "\n" + PAYLOAD)
        self.assertIn(PAYLOAD, body)
        self.assertIn("truncated", body)

    def test_flake_log_is_not_in_pr_and_summary_is_literal(self):
        log = "NIX_BUILD_NOISE unpacking flake inputs\n" * 10000 + PAYLOAD
        Path(self.env["TEST_LOG"]).write_text(log)
        result = self.successful_step(self.flake, "Run flake update")
        self.assertEqual(result.stdout, log.encode())
        self.assertEqual(self.output.read_text(), "")
        (self.runner / "flake-update-summary.txt").write_text("- nixpkgs changed\n" + PAYLOAD)
        self.successful_step(self.flake, "Generate PR body")
        body = self.post_body(self.flake)
        self.assertIn(PAYLOAD, body)
        self.assertNotIn("NIX_BUILD_NOISE", body)
        self.assertIn(RUN_URL, body)
        self.assertEqual((self.runner / "flake-update.txt").read_text(), log)

    def test_flake_large_utf8_summary_is_bounded_and_preserved(self):
        summary = "- **café**: `before` → `after`\n" * 10000 + PAYLOAD
        path = self.runner / "flake-update-summary.txt"
        path.write_text(summary)
        self.successful_step(self.flake, "Generate PR body")
        body = self.post_body(self.flake)
        self.assertIn("Summary truncated", body)
        self.assertIn(PAYLOAD, body)
        self.assertEqual(path.read_text(), summary)

    def test_flake_compare_summary_goes_to_file(self):
        node = {"locked": {"rev": "b" * 40, "type": "github", "owner": "example", "repo": "flake"}}
        before = {"locks": {"nodes": {"root": {"inputs": {"nixpkgs": "nixpkgs"}}, "nixpkgs": {"locked": {"rev": "a" * 40}}}}}
        after = {"locks": {"nodes": {"root": before["locks"]["nodes"]["root"], "nixpkgs": node}}}
        before_path = self.root / "before.json"
        before_path.write_text(json.dumps(before))
        Path(self.env["TEST_METADATA"]).write_text(json.dumps(after))
        # Isolate the existing fixed metadata path while executing the real script.
        step = self.step(self.flake, "Generate input diff summary")
        step["run"] = step["run"].replace("/tmp/flake-metadata-before.json", str(before_path))
        self.successful_step(self.flake, "Generate input diff summary")
        summary = (self.runner / "flake-update-summary.txt").read_text()
        self.assertIn(f"https://github.com/example/flake/compare/{'a' * 40}...{'b' * 40}", summary)
        self.assertIn("**nixpkgs**", summary)
        self.assertEqual(self.output.read_text(), "")

    def test_update_failures_stream_and_preserve_diagnostics(self):
        log = "update failed\n" + PAYLOAD
        Path(self.env["TEST_LOG"]).write_text(log)
        self.env["TEST_STATUS"] = "37"
        for steps, kind in [(self.cargo, "cargo"), (self.flake, "flake")]:
            with self.subTest(workflow=kind):
                result = self.run_step(steps, f"Run {kind} update")
                self.assertEqual(result.returncode, 37)
                self.assertEqual(result.stdout, log.encode())
                self.assertEqual((self.runner / f"{kind}-update.txt").read_text(), log)
                self.assertEqual(self.output.read_text(), "")

    def test_report_write_failure_fails_update(self):
        Path(self.env["TEST_LOG"]).write_text("successful update\n")
        for steps, kind in [(self.cargo, "cargo"), (self.flake, "flake")]:
            with self.subTest(workflow=kind):
                result = self.run_step(steps, f"Run {kind} update", {"REPORT": str(self.root / "missing" / "report")})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"No such file or directory", result.stderr)

    def test_composite_keeps_inline_body_compatibility(self):
        self.context["inputs.pr-body"] = PAYLOAD
        self.successful_step(self.composite, "Create or update PR")
        body = Path(self.env["TEST_GH_BODY"]).read_text()
        self.assertTrue(body.startswith(PAYLOAD))
        self.assertIn("<!-- hold-until: 2026-10-10 -->", body)
        self.assertFalse((self.root / "INJECTED").exists())

    def test_composite_file_takes_precedence_and_edit_uses_body_file(self):
        path = self.runner / "input body.md"
        path.write_text(PAYLOAD)
        self.context["inputs.pr-body"] = "ignored inline body"
        self.context["inputs.pr-body-file"] = str(path)
        self.env["TEST_EXISTING_PR"] = "42"
        self.successful_step(self.composite, "Create or update PR")
        body = Path(self.env["TEST_GH_BODY"]).read_text()
        self.assertTrue(body.startswith(PAYLOAD))
        self.assertNotIn("ignored inline body", body)
        self.assertEqual(path.read_text(), PAYLOAD)
        self.assertFalse((self.root / "INJECTED").exists())
        calls = Path(self.env["TEST_GH_CALLS"]).read_text()
        self.assertIn("pr\nedit\n42\n--body-file\n", calls)
        self.assertIn("pr\ncomment\n42\n--body\nUpdated with latest results.\n", calls)
        self.assertIn("number=42\n", self.output.read_text())

    def test_composite_missing_file_does_not_post(self):
        self.context["inputs.pr-body-file"] = str(self.runner / "missing.md")
        result = self.run_step(self.composite, "Create or update PR")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(Path(self.env["TEST_GH_CALLS"]).exists())

    def test_no_changes_still_skips_publish_steps(self):
        self.successful_step(self.composite, "Check for changes")
        self.assertEqual(self.output.read_text(), "changed=false\n")
        for name in ["Commit and push", "Calculate hold date", "Create or update PR"]:
            self.assertEqual(self.step(self.composite, name)["if"], "steps.check.outputs.changed == 'true'")


if __name__ == "__main__":
    unittest.main(verbosity=2)
