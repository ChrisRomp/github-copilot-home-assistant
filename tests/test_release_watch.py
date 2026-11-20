"""Tests for the HA release watch script used by GitHub Actions."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "ha_release_watch.py"
_spec = importlib.util.spec_from_file_location("ha_release_watch", SCRIPT)
watch = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = watch
_spec.loader.exec_module(watch)

SHA = "a" * 40
SHA2 = "b" * 40
RUN = "https://github.com/o/r/actions/runs/1"
WORKFLOW = "https://github.com/o/r/actions/workflows/ha-release-watch.yml"


def _files(requires=">=3.14.2", yanked=False):
    return [
        {"packagetype": "bdist_wheel", "requires_python": requires, "yanked": yanked}
    ]


def test_version_ordering_is_numeric():
    versions = [
        "2026.12.0",
        "2026.12.0rc0",
        "2026.12.0b10",
        "2026.12.0b9",
        "2027.1.0b0",
    ]
    assert sorted(versions, key=watch.parse_version) == [
        "2026.12.0b9",
        "2026.12.0b10",
        "2026.12.0rc0",
        "2026.12.0",
        "2027.1.0b0",
    ]
    assert watch.parse_version("2026.13.0") is None
    assert watch.parse_version("2026.12.0.dev1") is None


def test_select_targets_latest_stable_and_newer_month_beta():
    releases = {
        "2026.11.2": _files(),
        "2026.11.3": [],  # no files yet
        "2026.11.4": _files(yanked=True),
        "2026.11.0b6": _files(),  # older month line beta is ignored
        "2026.12.0b9": _files(),
        "2026.12.0b10": _files(requires=">=3.15"),
        "0.99.1": _files(),
    }
    assert watch.select_targets(releases) == [
        {
            "version": "2026.11.2",
            "channel": "stable",
            "line": "2026.11",
            "python": "3.14",
        },
        {
            "version": "2026.12.0b10",
            "channel": "beta",
            "line": "2026.12",
            "python": "3.15",
        },
    ]
    # Once the month ships, its betas are no longer tested; the year rolls over.
    releases["2026.12.0"] = _files()
    releases["2027.1.0b0"] = _files()
    assert [t["version"] for t in watch.select_targets(releases)] == [
        "2026.12.0",
        "2027.1.0b0",
    ]


def test_select_targets_override_is_validated():
    releases = {"2026.12.0b1": _files(), "2026.12.0b2": _files(yanked=True)}
    assert watch.select_targets(releases, "2026.12.0b1")[0]["channel"] == "beta"
    for bad in ("2026.12.0b2", "2026.12.0b3", "1.0; rm -rf /"):
        with pytest.raises(ValueError):
            watch.select_targets(releases, bad)


def test_python_for_intersects_with_project_minimum():
    assert watch.python_for(None) == "3.14"
    assert watch.python_for(">=3.13") == "3.14"
    assert watch.python_for(">=3.15.1,<3.16") == "3.15"
    with pytest.raises(ValueError):
        watch.python_for("<3.14")
    with pytest.raises(ValueError):
        watch.python_for("~=3.14")


def test_validate_result_rejects_and_sanitizes():
    base = {
        "version": "2026.12.0b1",
        "sha": SHA,
        "python": "3.14",
        "outcome": "failure",
        "phase": "test",
        "failing_tests": ["tests.test_x::test_a[`$(id)`]"],
        "failing_total": 1,
    }
    result = watch.validate_result(base)
    assert result["failing_tests"] == ["tests.test_x::test_a[___id__]"]
    assert result["line"] == "2026.12"
    for key, value in (
        ("version", "2026.12.0b1\n"),
        ("sha", "abc"),
        ("outcome", "maybe"),
        ("phase", "deploy"),
        ("python", "3.14; x"),
    ):
        assert watch.validate_result({**base, key: value}) is None


def test_failing_tests_reads_junit(tmp_path):
    junit = tmp_path / "junit.xml"
    junit.write_text(
        "<testsuites><testsuite>"
        '<testcase classname="tests.test_a" name="test_ok"/>'
        '<testcase classname="tests.test_a" name="test_bad"><failure/></testcase>'
        '<testcase classname="tests.test_b" name="test_err"><error/></testcase>'
        "</testsuite></testsuites>"
    )
    assert watch.failing_tests(junit) == [
        "tests.test_a::test_bad",
        "tests.test_b::test_err",
    ]
    assert watch.failing_tests(tmp_path / "missing.xml") == []


class FakeGitHub:
    """In-memory stand-in for the GitHub issues API."""

    def __init__(self):
        self.issues: dict[int, dict] = {}
        self.timelines: dict[int, list] = {}
        self.calls: list[tuple] = []
        self.fail_paths: set[str] = set()
        self.ignore_assignees = False
        self.next_number = 100

    def request(self, method, path, body=None, params=None):
        self.calls.append((method, path, body))
        if path in self.fail_paths:
            raise watch.GitHubError(403)
        if method == "POST" and path == "/issues":
            self.next_number += 1
            issue = {
                "number": self.next_number,
                "state": "open",
                "user": {"login": "github-actions[bot]"},
                "assignees": [],
                **body,
            }
            self.issues[self.next_number] = issue
            return issue
        match = re.fullmatch(r"/issues/(\d+)/assignees", path)
        if match and method == "POST":
            if self.ignore_assignees:
                return {"assignees": []}
            return {"assignees": [{"login": "Copilot"}]}
        match = re.fullmatch(r"/issues/(\d+)", path)
        if match:
            issue = self.issues[int(match.group(1))]
            if method == "PATCH":
                issue.update(body)
            return issue
        return {}

    def paginate(self, path, params, max_pages):
        match = re.fullmatch(r"/issues/(\d+)/timeline", path)
        if match:
            return self.timelines.get(int(match.group(1)), [])
        return list(self.issues.values())


def _result(version, outcome="failure", tests=("tests.t::test_a",), phase="test"):
    return watch.validate_result(
        {
            "version": version,
            "sha": SHA,
            "python": "3.14",
            "outcome": outcome,
            "phase": phase if outcome == "failure" else None,
            "failing_tests": list(tests) if outcome == "failure" else [],
        }
    )


def _reporter(api, copilot=True):
    return watch.Reporter(api, RUN, WORKFLOW, watch.load_issues(api), copilot)


def _run_copilot(api, ops, copilot=None):
    copilot = copilot or FakeGitHub()
    return watch.run_copilot_ops(ops, copilot, api, "o/r", "main", RUN, WORKFLOW)


def _state(api, number):
    return watch.parse_marker(api.issues[number]["body"])


def _copilot_pr(api, issue, pr, state="open"):
    api.timelines[issue] = [
        {
            "event": "cross-referenced",
            "source": {
                "issue": {
                    "number": pr,
                    "state": state,
                    "pull_request": {},
                    "user": {"login": "Copilot"},
                }
            },
        }
    ]


def test_failure_opens_issue_and_queues_assignment():
    api = FakeGitHub()
    reporter = _reporter(api)
    reporter.process(_result("2026.12.0b0"))

    issue = api.issues[101]
    assert issue["title"] == "HA compatibility: 2026.12.0b0 fails tests"
    assert issue["labels"] == ["ha-compat"]
    assert "install --mode watch 2026.12.0b0" in issue["body"]
    assert _state(api, 101)["versions"] == ["2026.12.0b0"]
    assert reporter.copilot_ops == [{"op": "assign", "issue": 101}]


def test_install_failures_and_disabled_copilot_do_not_assign():
    api = FakeGitHub()
    reporter = _reporter(api, copilot=False)
    reporter.process(_result("2026.12.0b0"))
    reporter = _reporter(api)
    reporter.process(_result("2026.11.2", tests=(), phase="install"))
    assert len(api.issues) == 2
    assert reporter.copilot_ops == []


def test_same_failure_on_later_beta_updates_issue_and_comments_on_copilot_pr():
    api = FakeGitHub()
    reporter = _reporter(api)
    reporter.process(_result("2026.12.0b0"))
    assert _run_copilot(api, reporter.copilot_ops) == 0
    _copilot_pr(api, 101, 7)

    # Retesting the version Copilot was assigned for does not mention it.
    reporter = _reporter(api)
    reporter.process(_result("2026.12.0b0"))
    assert reporter.copilot_ops == []

    reporter = _reporter(api)
    reporter.process(_result("2026.12.0b1"))
    assert len(api.issues) == 1
    assert _state(api, 101)["versions"] == ["2026.12.0b0", "2026.12.0b1"]
    assert reporter.copilot_ops == [
        {"op": "comment", "issue": 101, "pr": 7, "version": "2026.12.0b1"}
    ]
    copilot = FakeGitHub()
    assert _run_copilot(api, reporter.copilot_ops, copilot) == 0
    assert copilot.calls[-1][1] == "/issues/7/comments"
    assert copilot.calls[-1][2]["body"].startswith("@copilot ")

    # Each version is mentioned once.
    reporter = _reporter(api)
    reporter.process(_result("2026.12.0b1"))
    assert reporter.copilot_ops == []


def test_different_failure_opens_separate_issue():
    api = FakeGitHub()
    _reporter(api).process(_result("2026.12.0b0"))
    _reporter(api).process(_result("2026.12.0b1", tests=("tests.t::test_b",)))
    assert sorted(api.issues) == [101, 102]


def test_closed_copilot_pr_starts_new_issue():
    api = FakeGitHub()
    reporter = _reporter(api)
    reporter.process(_result("2026.12.0b0"))
    assert _run_copilot(api, reporter.copilot_ops) == 0
    _copilot_pr(api, 101, 7, state="closed")

    reporter = _reporter(api)
    reporter.process(_result("2026.12.0b1"))
    assert api.issues[101]["state"] == "closed"
    assert _state(api, 102)["previous"] == 101
    assert reporter.copilot_ops == [{"op": "assign", "issue": 102}]


def test_passing_versions_close_fixed_and_superseded_issues():
    api = FakeGitHub()
    _reporter(api).process(_result("2026.12.0b0"))
    _reporter(api).process(_result("2026.12.0b1", tests=("tests.t::test_b",)))
    _reporter(api).process(_result("2026.11.2", tests=("tests.t::test_c",)))

    _reporter(api).process(_result("2026.12.0b1", outcome="success"))
    assert api.issues[101]["state_reason"] == "not_planned"  # superseded
    assert api.issues[102]["state_reason"] == "completed"  # fixed
    assert api.issues[103]["state"] == "open"  # different month line


def test_tampered_marker_is_ignored():
    api = FakeGitHub()
    _reporter(api).process(_result("2026.12.0b0"))
    body = api.issues[101]["body"]
    api.issues[101]["body"] = body.replace('"phase": "test"', '"phase": "evil"')
    assert watch.load_issues(api) == []


def test_copilot_ops_record_success_and_retry_failures():
    api = FakeGitHub()
    reporter = _reporter(api)
    reporter.process(_result("2026.12.0b0"))
    copilot = FakeGitHub()
    copilot.fail_paths.add("/issues/101/assignees")

    args = ("o/r", "main", RUN, WORKFLOW)
    assert watch.run_copilot_ops(reporter.copilot_ops, copilot, api, *args) == 1
    assert _state(api, 101)["assigned"] is False

    copilot.fail_paths.clear()
    copilot.ignore_assignees = True  # GitHub can silently ignore the assignee
    assert watch.run_copilot_ops(reporter.copilot_ops, copilot, api, *args) == 1
    assert _state(api, 101)["assigned"] is False

    copilot.ignore_assignees = False
    assert watch.run_copilot_ops(reporter.copilot_ops, copilot, api, *args) == 0
    assert _state(api, 101)["assigned"] is True
    method, path, body = copilot.calls[-1]
    assert (method, path) == ("POST", "/issues/101/assignees")
    assert body["assignees"] == ["copilot-swe-agent[bot]"]
    assert body["agent_assignment"]["base_branch"] == "main"

    # A repeated operation does not assign again.
    calls = len(copilot.calls)
    watch.run_copilot_ops(reporter.copilot_ops, copilot, api, *args)
    assert len(copilot.calls) == calls


def test_result_command_writes_validated_artifact(tmp_path):
    (tmp_path / "phase").write_text("test\n")
    out = tmp_path / "result.json"
    watch.main(
        [
            "result",
            "--version",
            "2026.12.0b0",
            "--sha",
            SHA2,
            "--python",
            "3.14",
            "--outcome",
            "failure",
            "--phase-file",
            str(tmp_path / "phase"),
            "--junit",
            str(tmp_path / "missing.xml"),
            "--out",
            str(out),
        ]
    )
    assert json.loads(out.read_text())["phase"] == "test"
