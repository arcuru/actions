# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Local regressions, not evidence of GitHub runner support for $/."""

import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / ".github/actions/scan-pins/pins-lib.sh"
VERIFY = ROOT / ".github/actions/verify-pins/verify.sh"
SHA = "a" * 40


def load(relative):
    # BaseLoader preserves GitHub's `on` key instead of YAML 1.1's boolean.
    return yaml.load((ROOT / relative).read_text(), Loader=yaml.BaseLoader)


def run(command, *, cwd=ROOT, env=None, input=None):
    return subprocess.run(
        command, cwd=cwd, env=env, input=input, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )


def parse(content):
    result = run(
        ["bash", "-c", 'source "$1"; pins_parse_content fixture.yml "$(cat)" | jq -s .',
         "bash", str(LIBRARY)], input=content,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class PinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="self-reference-test-")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.bin = self.work / "bin"
        self.bin.mkdir()
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        RUNNER_TEMP=str(self.work), GITHUB_OUTPUT=str(self.work / "output"),
                        GITHUB_REPOSITORY="example/actions", GITHUB_SHA=SHA, HEAD_REF="")
        self.calls = self.work / "calls"
        self.env["GH_CALLS"] = str(self.calls)
        self.stub_gh('echo "Unexpected API call" >&2; exit 97')

    def stub_gh(self, body):
        stub = self.bin / "gh"
        stub.write_text('#!/usr/bin/env bash\nset -eu\nprintf "%s\\n" "$*" >> "$GH_CALLS"\n' + body + "\n")
        stub.chmod(0o755)

    def verify(self, pins):
        result = run(["bash", str(VERIFY)], cwd=self.work, env=self.env,
                     input=json.dumps(pins))
        self.assertEqual(result.returncode, 0, result.stderr)
        values = {}
        for line in (self.work / "output").read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key in {"status", "critical", "warning", "info", "checked", "total"}:
                values[key] = value
        return values, result.stdout

    def test_self_quotes_comments_and_workflow_paths(self):
        for value in ["$/.github/actions/outer", "'$/.github/actions/outer'",
                      '"$/.github/actions/outer" # explanatory comment',
                      "$/.github/workflows/reusable.yml"]:
            with self.subTest(value=value):
                pins = parse(f"  - uses: {value}\n")
                self.assertEqual(len(pins), 1)
                self.assertEqual(pins[0]["ref_kind"], "self")
                self.assertEqual(pins[0]["class"], "self")
                for field in ["owner", "repo", "ref"]:
                    self.assertEqual(pins[0][field], "")
                self.assertTrue(pins[0]["subpath"].startswith("/.github/"))

    def test_non_references_are_not_scanned(self):
        self.assertEqual(parse('''# - uses: $/.github/actions/ignored
  # uses: owner/repo@main
  run: echo 'uses: $/.github/actions/ignored'
  note: uses: owner/repo@main
  - uses: ./.github/actions/local
  - uses: docker://alpine:3
'''), [])

    def test_mutable_replacement_is_not_self(self):
        pins = parse("uses: example/actions/.github/actions/outer@main # main\n")
        self.assertEqual(pins[0]["ref_kind"], "unpinned")
        values, report = self.verify(pins)
        self.assertEqual(values["status"], "critical")
        self.assertEqual(values["critical"], "1")
        self.assertIn("is not pinned to a commit", report)

    def test_self_with_ref_is_not_exempt(self):
        pins = parse("uses: $/.github/actions/outer@main\n")
        self.assertEqual(len(pins), 1)
        self.assertNotEqual(pins[0]["ref_kind"], "self")
        values, _ = self.verify(pins)
        self.assertEqual(values["status"], "critical")

    def test_sha_pin_and_version_comment_are_preserved(self):
        pin = parse(f'uses: "owner/repo/path@{SHA}" # v1.2.3\n')[0]
        self.assertEqual((pin["owner"], pin["repo"], pin["subpath"], pin["ref"],
                          pin["tag"], pin["class"], pin["ref_kind"]),
                         ("owner", "repo", "/path", SHA, "v1.2.3", "immutable", "sha"))

    def test_self_verification_counts_distinct_paths_without_api(self):
        pins = parse('''uses: $/.github/actions/outer
uses: $/.github/actions/outer
uses: $/.github/actions/outer/nested
uses: $/.github/workflows/reusable.yml
''')
        values, report = self.verify(pins)
        self.assertEqual(values, {"status": "ok", "critical": "0", "warning": "0",
                                  "info": "0", "checked": "3", "total": "4"})
        self.assertIn("Verified 3 distinct references across 4 `uses:` lines", report)
        self.assertFalse(self.calls.exists(), "Self references must not make upstream lookups")

    def test_empty_scan_is_not_a_clean_pass(self):
        values, report = self.verify([])
        self.assertEqual(values["status"], "warning")
        self.assertIn("no `uses:` references were found", report)

    def test_external_pin_still_gets_path_tag_and_advisory_checks(self):
        self.stub_gh('''case "$2" in
  repos/owner/repo/contents/action.yml?ref=*) echo '{}' ;;
  repos/owner/repo/git/ref/tags/v1.2.3)
    printf '{"object":{"type":"commit","sha":"%s"}}\\n' "$EXPECTED_SHA" ;;
  advisories*) echo '[]' ;;
  *) echo "Unexpected API call: $*" >&2; exit 97 ;;
esac''')
        self.env["EXPECTED_SHA"] = "b" * 40
        pins = parse(f"uses: $/.github/actions/outer\nuses: owner/repo@{SHA} # v1.2.3\n")
        values, report = self.verify(pins)
        self.assertEqual(values["checked"], "2")
        self.assertEqual(values["status"], "critical")
        self.assertIn("was re-pointed upstream", report)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(len(calls), 3)
        self.assertTrue(any("contents/action.yml" in call for call in calls))
        self.assertTrue(any("git/ref/tags/v1.2.3" in call for call in calls))
        self.assertTrue(any("affects=owner/repo@v1.2.3" in call for call in calls))

    def test_local_and_remote_discovery_include_nested_manifests(self):
        paths = [".github/workflows/test.yml", ".forgejo/workflows/test.yaml",
                 ".github/actions/outer/action.yml",
                 ".github/actions/outer/nested/action.yaml"]
        for path in paths:
            target = self.work / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("uses: $/.github/actions/outer\n")
        ignored = self.work / ".github/workflows/subdir/ignored.yml"
        ignored.parent.mkdir(parents=True)
        ignored.write_text("uses: $/.github/actions/ignored\n")
        local = run(["bash", "-c", 'source "$1"; pins_scan_local', "bash", str(LIBRARY)],
                    cwd=self.work, env=self.env)
        self.assertEqual(local.returncode, 0, local.stderr)
        self.assertEqual(sorted(pin["file"] for pin in json.loads(local.stdout)), sorted(paths))
        self.env["TREE_JSON"] = json.dumps({"truncated": False, "tree": [
            {"type": "blob", "path": path} for path in paths + [str(ignored.relative_to(self.work))]
        ]})
        self.env["CONTENT_BASE64"] = base64.b64encode(b"uses: $/.github/actions/outer\n").decode()
        self.stub_gh('''case "$2" in
  repos/example/actions/git/trees/*) printf '%s' "$TREE_JSON" ;;
  repos/example/actions/contents/*) printf '%s' "$CONTENT_BASE64" ;;
  *) exit 97 ;;
esac''')
        remote = run(["bash", "-c", 'source "$1"; pins_scan_remote example/actions test-ref',
                      "bash", str(LIBRARY)], cwd=self.work, env=self.env)
        self.assertEqual(remote.returncode, 0, remote.stderr)
        self.assertEqual(sorted(json.loads(local.stdout), key=lambda p: p["file"]),
                         sorted(json.loads(remote.stdout), key=lambda p: p["file"]))

    def test_updater_skips_self_without_rewriting_or_api_lookups(self):
        content = "uses: $/.github/actions/outer\nuses: $/.github/workflows/reusable.yml\n"
        target = self.work / "fixture.yml"
        target.write_text(content)
        update = next(s for s in load(".github/workflows/actions-update.yml")
                      ["jobs"]["actions-update"]["steps"] if s.get("id") == "update")
        self.env["SCANNED_PINS"] = json.dumps(parse(content))
        result = run(["bash", "-e", "-c", update["run"]], cwd=self.work, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skipped 2 self-repository", result.stdout)
        self.assertEqual(target.read_text(), content)
        self.assertIn("No action updates available.", (self.work / "output").read_text())
        self.assertFalse(self.calls.exists())

    def test_remote_scan_failure_is_not_an_empty_success(self):
        for body in ["exit 1", '''case "$2" in
  */git/trees/*) echo '{"truncated":true,"tree":[]}' ;;
  *) exit 97 ;;
esac''', '''case "$2" in
  */git/trees/*) echo '{"truncated":false,"tree":[{"type":"blob","path":".github/workflows/test.yml"}]}' ;;
  *) exit 1 ;;
esac''']:
            with self.subTest(body=body):
                self.stub_gh(body)
                result = run(["bash", "-c", 'source "$1"; pins_scan_remote example/actions test-ref',
                              "bash", str(LIBRARY)], cwd=self.work, env=self.env)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")


class LintTests(unittest.TestCase):
    def setUp(self):
        ci = load(".github/workflows/ci.yml")
        script = next(s["run"] for s in ci["jobs"]["actionlint"]["steps"]
                      if s.get("name") == "Run actionlint")
        words = shlex.split("\n".join(line for line in script.splitlines()
                                    if not line.lstrip().startswith("#")))
        # Test the actual CI flags, not a second copy of the workaround.
        self.ignores = []
        for i, word in enumerate(words):
            if word == "-ignore":
                self.ignores.extend([word, words[i + 1]])
        self.assertTrue(self.ignores)

    def lint(self, body, *, ignore=True):
        with tempfile.TemporaryDirectory(prefix="actionlint-test-") as temp:
            path = Path(temp) / "fixture.yml"
            path.write_text("name: Fixture\non: workflow_dispatch\npermissions:\n  contents: read\njobs:\n" + body)
            return run(["actionlint", "-oneline", *(self.ignores if ignore else []), str(path)])

    def test_valid_self_action_is_the_only_action_parse_error_ignored(self):
        body = "  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: $/.github/actions/probe\n"
        raw = self.lint(body, ignore=False)
        self.assertNotEqual(raw.returncode, 0)
        self.assertIn('specifying action "$/.github/actions/probe"', raw.stdout)
        result = self.lint(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_valid_self_reusable_call_has_only_its_format_error_ignored(self):
        body = "  test:\n    uses: $/.github/workflows/reusable.yml\n"
        raw = self.lint(body, ignore=False)
        self.assertNotEqual(raw.returncode, 0)
        self.assertIn('reusable workflow call "$/.github/workflows/reusable.yml"', raw.stdout)
        result = self.lint(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_external_workflow_ref_is_still_reported(self):
        result = self.lint("  test:\n    uses: owner/repo/.github/workflows/reusable.yml\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('reusable workflow call "owner/repo/', result.stdout)

    def test_reusable_call_job_errors_are_not_ignored(self):
        result = self.lint("  test:\n    runs-on: ubuntu-latest\n"
                           "    uses: $/.github/workflows/reusable.yml\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('when a reusable workflow is called', result.stdout)

    def test_self_reusable_with_empty_ref_is_not_ignored(self):
        result = self.lint("  test:\n    uses: $/.github/workflows/reusable.yml@\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('reusable workflow call', result.stdout)

    def test_missing_external_ref_is_still_reported(self):
        result = self.lint("  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('specifying action "actions/checkout"', result.stdout)

    def test_shellcheck_is_not_ignored_in_self_syntax_workflow(self):
        result = self.lint('  test:\n    runs-on: ubuntu-latest\n    steps:\n'
                           '      - uses: $/.github/actions/probe\n      - run: echo $UNQUOTED\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SC2086", result.stdout)

    def test_expression_validation_is_not_ignored(self):
        result = self.lint('  test:\n    runs-on: ubuntu-latest\n    steps:\n'
                           '      - uses: $/.github/actions/probe\n'
                           '      - run: echo ok\n        if: missing_context.value\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("undefined variable", result.stdout)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.workflow = load(".github/workflows/self-syntax-probe.yml")
        self.reusable = load(".github/workflows/self-syntax-probe-reusable.yml")
        self.outer = load(".github/actions/self-syntax-probe/action.yml")
        self.nested = load(".github/actions/self-syntax-probe/nested/action.yml")
        self.jobs = [self.workflow["jobs"]["probe"], self.reusable["jobs"]["probe"]]

    def test_both_probe_matrices_cover_all_required_runner_images(self):
        for job in self.workflow["jobs"].values():
            self.assertEqual(job["strategy"]["matrix"]["runner"],
                             ["ubuntu-latest", "ubicloud-standard-2", "ubicloud-standard-2-arm"])
            self.assertEqual(job["strategy"]["fail-fast"], "false")
        caller = self.workflow["jobs"]["reusable-probe"]
        self.assertEqual(caller["uses"], "$/.github/workflows/self-syntax-probe-reusable.yml")
        self.assertEqual(caller["with"]["runner"], "${{ matrix.runner }}")
        self.assertEqual(self.reusable["jobs"]["probe"]["runs-on"], "${{ inputs.runner }}")
        self.assertEqual(self.reusable["on"]["workflow_call"]["inputs"]["runner"]["required"], "true")

    def test_probe_permissions_owner_gates_and_timeouts_are_explicit(self):
        for workflow in [self.workflow, self.reusable]:
            self.assertEqual(workflow["permissions"], {"contents": "read"})
            for job in workflow["jobs"].values():
                self.assertEqual(job["if"], "github.repository_owner == 'arcuru'")
                self.assertEqual(job["permissions"], {"contents": "read"})
                self.assertNotIn("secrets", job)
                if "steps" in job:
                    self.assertEqual(job["timeout-minutes"], "5")

    def test_probe_has_no_checkout_and_output_chain_reaches_nested_action(self):
        for job in self.jobs:
            step = next(s for s in job["steps"] if s.get("id") == "probe")
            self.assertEqual(step["uses"], "$/.github/actions/self-syntax-probe")
            assertion = next(s for s in job["steps"] if s.get("name") == "Assert the probe executed")
            self.assertEqual(assertion["env"]["MARKER"], "${{ steps.probe.outputs.marker }}")
        for steps in [*(j["steps"] for j in self.jobs),
                      self.outer["runs"]["steps"], self.nested["runs"]["steps"]]:
            for step in steps:
                self.assertNotIn("checkout", step.get("uses", ""))
                self.assertNotIn("${{", step.get("run", ""))
        self.assertEqual(self.outer["outputs"]["marker"]["value"], "${{ steps.nested.outputs.marker }}")
        nested = next(s for s in self.outer["runs"]["steps"] if s.get("id") == "nested")
        self.assertEqual(nested["uses"], "$/.github/actions/self-syntax-probe/nested")
        self.assertEqual(self.nested["outputs"]["marker"]["value"], "${{ steps.mark.outputs.marker }}")

    def test_emitted_marker_satisfies_both_real_assertion_scripts(self):
        with tempfile.TemporaryDirectory(prefix="probe-test-") as temp:
            output = Path(temp) / "output"
            env = dict(os.environ, GITHUB_OUTPUT=str(output), RUNNER_OS="Linux", RUNNER_ARCH="ARM64")
            mark = next(s for s in self.nested["runs"]["steps"] if s.get("id") == "mark")
            result = run(["bash", "-e", "-c", mark["run"]], cwd=temp, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text(), "marker=self-syntax-ok\n")
            env["MARKER"] = output.read_text().strip().removeprefix("marker=")
            for job in self.jobs:
                assertion = next(s for s in job["steps"] if s.get("name") == "Assert the probe executed")
                result = run(["bash", "-e", "-c", assertion["run"]], cwd=temp, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("including nested composition", result.stdout)

    def test_missing_or_wrong_marker_fails_both_real_assertion_scripts(self):
        for marker in ["", "wrong-marker"]:
            for job in self.jobs:
                with self.subTest(marker=marker, job=job):
                    assertion = next(s for s in job["steps"] if s.get("name") == "Assert the probe executed")
                    env = dict(os.environ, MARKER=marker, RUNNER_OS="Linux", RUNNER_ARCH="X64")
                    result = run(["bash", "-e", "-c", assertion["run"]], env=env)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("::error::", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
