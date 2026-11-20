"""Test this integration against new Home Assistant releases from PyPI.

Subcommands used by CI and for local reproduction:

- ``install``: install HA and the test dependencies (``supported`` or ``watch`` mode).
- ``discover``: select the latest stable release and the active monthly beta.
- ``result``: summarize one test job as a small JSON artifact.
- ``report``: open, update, and close ``ha-compat`` issues from result artifacts.
- ``copilot``: run the Copilot assignments and pull request comments that ``report``
  queued. Only this subcommand receives ``COPILOT_ASSIGN_TOKEN``.

The script uses only the standard library so it runs before dependencies install.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPI_URL = "https://pypi.org/pypi/homeassistant/json"
PROJECT_MIN_PYTHON = (3, 14, 2)
CONTRACT_COMPONENTS = ("conversation", "camera", "image", "stream")

LABEL = "ha-compat"
ISSUE_AUTHOR = "github-actions[bot]"
COPILOT_ASSIGNEE = "copilot-swe-agent[bot]"
COPILOT_LOGINS = frozenset({"Copilot", "copilot-swe-agent", "copilot-swe-agent[bot]"})
COPILOT_INSTRUCTIONS = (
    "Fix the Home Assistant compatibility failure described in this issue. Follow "
    ".github/copilot-instructions.md, keep every CI matrix version passing, and do "
    "not widen the supported Home Assistant range."
)

VERSION_RE = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,3})(?:(b|rc)(\d{1,3}))?$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SIG_RE = re.compile(r"^[0-9a-f]{12}$")
PYTHON_RE = re.compile(r"^3\.\d{1,2}$")
MARKER_RE = re.compile(r"<!-- ha-compat:state (\{.*?\}) -->")
TEST_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_./:\[\]\-=, ]")
PHASES = ("install", "lint", "test")
PHASE_LABELS = {"install": "dependency install", "lint": "lint", "test": "tests"}
OUTCOMES = ("success", "failure", "skipped", "cancelled")
MAX_FAILING_TESTS = 50
MAX_TEST_ID = 200
MAX_RESULT_BYTES = 256 * 1024
MAX_ISSUE_PAGES = 5
MAX_TIMELINE_PAGES = 5


# Versions


def parse_version(version: str) -> tuple[int, int, int, int, int] | None:
    """Return a sortable key, ordering ``b`` < ``rc`` < final numerically."""
    match = VERSION_RE.fullmatch(version)
    if not match:
        return None
    year, month, patch, stage, number = match.groups()
    if not 1 <= int(month) <= 12:
        return None
    rank = {"b": 0, "rc": 1, None: 2}[stage]
    return int(year), int(month), int(patch), rank, int(number or 0)


def is_prerelease(version: str) -> bool:
    key = parse_version(version)
    return key is not None and key[3] < 2


def month_line(version: str) -> str:
    key = parse_version(version)
    if key is None:
        raise ValueError("invalid Home Assistant version")
    return f"{key[0]}.{key[1]}"


def _usable_files(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        file
        for file in files
        if not file.get("yanked")
        and file.get("packagetype") in ("bdist_wheel", "sdist")
    ]


def _python_tuple(text: str) -> tuple[int, int, int]:
    parts = [int(part) for part in text.split(".")] + [0, 0]
    return parts[0], parts[1], parts[2]


def python_for(requires_python: str | None) -> str:
    """Pick a ``major.minor`` Python satisfying HA and this project."""
    lower = PROJECT_MIN_PYTHON
    upper: tuple[int, int, int] | None = None
    clauses = (c.strip() for c in (requires_python or "").split(","))
    for clause in filter(None, clauses):
        match = re.fullmatch(r"(>=|>|<=|<)\s*(\d+(?:\.\d+){0,2})", clause)
        if not match:
            raise ValueError(f"unsupported requires_python clause: {clause!r}")
        operator, text = match.groups()
        bound = _python_tuple(text)
        if operator == ">=":
            lower = max(lower, bound)
        elif operator == ">":
            lower = max(lower, (bound[0], bound[1], bound[2] + 1))
        elif operator == "<":
            upper = bound if upper is None else min(upper, bound)
        elif operator == "<=":
            limit = (bound[0], bound[1], bound[2] + 1)
            upper = limit if upper is None else min(upper, limit)
    if upper is not None and lower >= upper:
        raise ValueError("no Python version satisfies Home Assistant and this project")
    return f"{lower[0]}.{lower[1]}"


def _target(version: str, files: list[dict[str, Any]]) -> dict[str, Any]:
    requires = next(
        (
            f["requires_python"]
            for f in _usable_files(files)
            if f.get("requires_python")
        ),
        None,
    )
    return {
        "version": version,
        "channel": "beta" if is_prerelease(version) else "stable",
        "line": month_line(version),
        "python": python_for(requires),
    }


def select_targets(
    releases: dict[str, list[dict[str, Any]]], override: str | None = None
) -> list[dict[str, Any]]:
    """Select the latest stable release and any newer monthly prerelease."""
    usable = {
        version: files
        for version, files in releases.items()
        if parse_version(version) and _usable_files(files)
    }
    if override:
        if parse_version(override) is None:
            raise ValueError("version must look like 2026.12.0 or 2026.12.0b1")
        if override not in usable:
            raise ValueError("version is not an installable PyPI release")
        return [_target(override, usable[override])]

    stable = [v for v in usable if not is_prerelease(v)]
    beta = [v for v in usable if is_prerelease(v)]
    latest_stable = max(stable, key=parse_version, default=None)
    latest_beta = max(beta, key=parse_version, default=None)
    targets = []
    if latest_stable:
        targets.append(_target(latest_stable, usable[latest_stable]))
    if latest_beta and (
        latest_stable is None
        or parse_version(latest_beta)[:2] > parse_version(latest_stable)[:2]
    ):
        targets.append(_target(latest_beta, usable[latest_beta]))
    return targets


# HTTP


class GitHubError(RuntimeError):
    def __init__(self, status: int) -> None:
        super().__init__(f"GitHub API returned HTTP {status}")
        self.status = status


def _fetch_json(request: urllib.request.Request, attempts: int = 3) -> Any:
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as err:
            if err.code < 500 or attempt == attempts - 1:
                raise GitHubError(err.code) from None
        except (urllib.error.URLError, TimeoutError):
            if attempt == attempts - 1:
                raise
        time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


class GitHub:
    def __init__(self, token: str, repo: str) -> None:
        self.token = token
        api_url = os.environ.get("GITHUB_API_URL") or "https://api.github.com"
        self.repo_url = f"{api_url}/repos/{repo}"

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.repo_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        return _fetch_json(request)

    def paginate(
        self, path: str, params: dict[str, Any], max_pages: int
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            batch = self.request(
                "GET", path, params={**params, "per_page": 100, "page": page}
            )
            items.extend(batch or [])
            if not batch or len(batch) < 100:
                break
        return items


# Issue state


@dataclass
class Tracked:
    number: int
    open: bool
    state: dict[str, Any]
    assignees: set[str] = field(default_factory=set)


def _version_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(
        isinstance(v, str) and parse_version(v) for v in value
    ):
        return None
    return list(dict.fromkeys(value))


def parse_marker(body: str | None) -> dict[str, Any] | None:
    """Parse and strictly validate the state stored in an issue body."""
    match = MARKER_RE.search(body or "")
    if not match:
        return None
    try:
        raw = json.loads(match.group(1))
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    versions = _version_list(raw.get("versions"))
    commented = _version_list(raw.get("commented", []))
    tests = raw.get("failing_tests", [])
    total = raw.get("failing_total", 0)
    previous = raw.get("previous")
    valid = (
        versions
        and commented is not None
        and isinstance(raw.get("sig"), str)
        and SIG_RE.fullmatch(raw["sig"])
        and raw.get("phase") in PHASES
        and isinstance(raw.get("sha"), str)
        and SHA_RE.fullmatch(raw["sha"])
        and isinstance(raw.get("python"), str)
        and PYTHON_RE.fullmatch(raw["python"])
        and isinstance(tests, list)
        and len(tests) <= MAX_FAILING_TESTS
        and all(isinstance(t, str) for t in tests)
        and isinstance(total, int)
        and total >= 0
        and (previous is None or (isinstance(previous, int) and previous > 0))
    )
    if not valid:
        return None
    lines = {month_line(v) for v in versions}
    if len(lines) != 1:
        return None
    latest = max(versions, key=parse_version)
    state = {
        "sig": raw["sig"],
        "line": lines.pop(),
        "versions": versions,
        "channel": "beta" if is_prerelease(latest) else "stable",
        "phase": raw["phase"],
        "sha": raw["sha"],
        "python": raw["python"],
        "failing_tests": [sanitize_test_id(t) for t in tests],
        "failing_total": max(total, len(tests)),
        "assigned": raw.get("assigned") is True,
        "commented": commented,
    }
    if previous is not None:
        state["previous"] = previous
    return state


def load_issues(api: Any) -> list[Tracked]:
    issues = api.paginate(
        "/issues", {"labels": LABEL, "state": "all"}, max_pages=MAX_ISSUE_PAGES
    )
    tracked = []
    for issue in issues:
        if "pull_request" in issue:
            continue
        if (issue.get("user") or {}).get("login") != ISSUE_AUTHOR:
            continue
        state = parse_marker(issue.get("body"))
        if state is None:
            continue
        tracked.append(
            Tracked(
                number=int(issue["number"]),
                open=issue.get("state") == "open",
                state=state,
                assignees={a.get("login", "") for a in issue.get("assignees") or []},
            )
        )
    return tracked


def retest_versions(issues: list[Tracked]) -> set[str]:
    return {v for issue in issues if issue.open for v in issue.state["versions"]}


# Results


def failing_tests(junit_path: Path) -> list[str]:
    try:
        root = ET.parse(junit_path).getroot()
    except (OSError, ET.ParseError):
        return []
    failed = set()
    for case in root.iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            name = f"{case.get('classname', '')}::{case.get('name', '')}"
            failed.add(name.strip(":"))
    return sorted(failed)


def sanitize_test_id(test_id: str) -> str:
    return TEST_ID_UNSAFE.sub("_", test_id)[:MAX_TEST_ID]


def signature(phase: str | None, tests: list[str]) -> str:
    payload = json.dumps([phase, sorted(set(tests))])
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def validate_result(data: Any) -> dict[str, Any] | None:
    """Return a validated result, or ``None`` when it is not trustworthy data."""
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    if not isinstance(version, str) or parse_version(version) is None:
        return None
    if data.get("outcome") not in OUTCOMES:
        return None
    if not isinstance(data.get("sha"), str) or not SHA_RE.fullmatch(data["sha"]):
        return None
    python = data.get("python")
    if not isinstance(python, str) or not PYTHON_RE.fullmatch(python):
        return None
    phase = data.get("phase")
    sig = data.get("sig")
    if data["outcome"] == "failure" and (
        phase not in PHASES or not isinstance(sig, str) or not SIG_RE.fullmatch(sig)
    ):
        return None
    tests = data.get("failing_tests", [])
    if not isinstance(tests, list) or not all(isinstance(t, str) for t in tests):
        return None
    total = data.get("failing_total", len(tests))
    if not isinstance(total, int) or total < 0:
        return None
    clean = sorted({sanitize_test_id(t) for t in tests})[:MAX_FAILING_TESTS]
    return {
        "version": version,
        "channel": "beta" if is_prerelease(version) else "stable",
        "line": month_line(version),
        "sha": data["sha"],
        "python": python,
        "outcome": data["outcome"],
        "phase": phase if phase in PHASES else None,
        "sig": sig if data["outcome"] == "failure" else None,
        "failing_tests": clean,
        "failing_total": max(total, len(clean)),
    }


# Issue rendering


def render_body(state: dict[str, Any], run_url: str, workflow_url: str) -> str:
    stored = {k: v for k, v in state.items() if k != "line"}
    latest = max(state["versions"], key=parse_version)
    versions = ", ".join(f"`{v}`" for v in sorted(state["versions"], key=parse_version))
    phase = PHASE_LABELS[state["phase"]]
    lines = [
        f"<!-- ha-compat:state {json.dumps(stored, sort_keys=True)} -->",
        "## Home Assistant compatibility failure",
        "",
        f"The [HA release watch]({workflow_url}) found that this integration fails "
        f"{phase} on Home Assistant `{latest}` ({state['channel']}).",
        "",
        "| | |",
        "| --- | --- |",
        f"| Affected versions | {versions} |",
        f"| Failing step | {phase} |",
        f"| Tested commit | `{state['sha']}` |",
        f"| Python | {state['python']} |",
        f"| Latest run | {run_url} |",
        f"| Failure signature | `{state['sig']}` |",
        "",
    ]
    if state["failing_tests"]:
        listed = len(state["failing_tests"])
        lines += ["### Failing tests", "", "```text", *state["failing_tests"], "```"]
        if state["failing_total"] > listed:
            lines.append(f"Showing {listed} of {state['failing_total']} failing tests.")
        lines.append("")
    lines += [
        "### Reproduce",
        "",
        f"Use Python {state['python']} in a new virtual environment:",
        "",
        "```sh",
        f"python .github/scripts/ha_release_watch.py install --mode watch {latest}",
        "HA_COPILOT_HA_TESTS=1 python -m pytest -q -rs",
        "```",
        "",
        "Watch mode installs this Home Assistant version even when it is outside "
        "the supported range in `pyproject.toml`.",
        "",
        "### Task",
        "",
    ]
    if state["phase"] == "install":
        lines.append(
            "The dependencies do not install together. This usually means Home "
            "Assistant pins a package that conflicts with the GitHub Copilot SDK pin "
            "or a test dependency. A maintainer needs to review this; do not upgrade "
            "the SDK pin without reverifying the native runtime contracts."
        )
    else:
        lines += [
            f"Make the integration pass on Home Assistant `{latest}` and on every "
            "version in the CI matrix (`.github/workflows/validate.yml`). Follow "
            "`.github/copilot-instructions.md`.",
            "",
            "- Gate behavior on probed HA capability rather than version checks.",
            "- Do not change the supported Home Assistant range in `pyproject.toml`, "
            "`manifest.json`, or `hacs.json`, or the CI matrix, only to make a beta "
            "pass.",
            "- When a test double no longer matches HA, update it to match the real "
            "HA API instead of weakening assertions.",
            "- Do not upgrade the GitHub Copilot SDK pin.",
        ]
    if state.get("previous"):
        lines += ["", f"Previous issue: #{state['previous']}"]
    return "\n".join(lines) + "\n"


def issue_title(state: dict[str, Any]) -> str:
    return (
        f"HA compatibility: {state['versions'][0]} fails {PHASE_LABELS[state['phase']]}"
    )


# Reporting


class Reporter:
    def __init__(
        self,
        api: Any,
        run_url: str,
        workflow_url: str,
        issues: list[Tracked],
        copilot_enabled: bool,
    ) -> None:
        self.api = api
        self.run_url = run_url
        self.workflow_url = workflow_url
        self.issues = issues
        self.copilot_enabled = copilot_enabled
        self.copilot_ops: list[dict[str, Any]] = []

    def ensure_label(self) -> None:
        try:
            self.api.request(
                "POST",
                "/labels",
                {
                    "name": LABEL,
                    "color": "d93f0b",
                    "description": "Home Assistant release compatibility failure",
                },
            )
        except GitHubError as err:
            if err.status != 422:
                raise

    def copilot_prs(self, issue: Tracked) -> list[tuple[int, bool]]:
        """Return Copilot-authored pull requests that reference the issue."""
        events = self.api.paginate(
            f"/issues/{issue.number}/timeline", {}, max_pages=MAX_TIMELINE_PAGES
        )
        prs: dict[int, bool] = {}
        for event in events:
            source = (event.get("source") or {}).get("issue") or {}
            if (
                event.get("event") == "cross-referenced"
                and "pull_request" in source
                and (source.get("repository_url") or "").lower()
                == self.api.repo_url.lower()
                and (source.get("user") or {}).get("login") in COPILOT_LOGINS
            ):
                prs[int(source["number"])] = source.get("state") == "open"
        return sorted(prs.items())

    def process(self, result: dict[str, Any]) -> None:
        if result["outcome"] == "success":
            self._close_passing(result)
        elif result["outcome"] == "failure":
            self._record_failure(result)

    def _close_passing(self, result: dict[str, Any]) -> None:
        version = result["version"]
        key = parse_version(version)
        for issue in self.issues:
            if not issue.open or issue.state["line"] != result["line"]:
                continue
            newest = max(parse_version(v) for v in issue.state["versions"])
            if key == newest:
                note, reason = "Closing as fixed.", "completed"
            elif key > newest:
                note = f"Closing as superseded: the newer `{version}` passes."
                reason = "not_planned"
            else:
                continue
            self._comment(
                issue.number,
                f"Home Assistant `{version}` passes on `{result['sha'][:7]}` "
                f"({self.run_url}). {note}",
            )
            self._close(issue, reason)
            print(f"Closed issue #{issue.number} ({reason}).")

    def _record_failure(self, result: dict[str, Any]) -> None:
        sig = result["sig"]
        candidates = [
            issue
            for issue in self.issues
            if issue.open
            and issue.state["line"] == result["line"]
            and issue.state["sig"] == sig
        ]
        assignable = result["phase"] in ("lint", "test")
        if not candidates:
            self._create(result, sig, assignable)
            return
        match = max(candidates, key=lambda issue: issue.number)

        prs = self.copilot_prs(match) if match.state["assigned"] else []
        open_pr = next((number for number, is_open in prs if is_open), None)
        if prs and open_pr is None:
            new = self._create(result, sig, assignable, previous=match.number)
            self._comment(
                match.number,
                "Copilot's pull request for this issue is closed, but Home Assistant "
                f"`{result['version']}` still fails the same way. Continuing in "
                f"#{new.number}.",
            )
            self._close(match, "not_planned")
            return

        state = match.state
        if result["version"] not in state["versions"]:
            state["versions"].append(result["version"])
        latest = max(state["versions"], key=parse_version)
        state.update(
            sha=result["sha"],
            python=result["python"],
            channel="beta" if is_prerelease(latest) else "stable",
            failing_total=result["failing_total"],
        )
        self.api.request(
            "PATCH",
            f"/issues/{match.number}",
            {"body": render_body(state, self.run_url, self.workflow_url)},
        )
        print(f"Updated issue #{match.number}.")
        if not (self.copilot_enabled and assignable):
            return
        if not state["assigned"]:
            self.copilot_ops.append({"op": "assign", "issue": match.number})
        elif open_pr and result["version"] not in state["commented"]:
            self.copilot_ops.append(
                {
                    "op": "comment",
                    "issue": match.number,
                    "pr": open_pr,
                    "version": result["version"],
                }
            )

    def _create(
        self,
        result: dict[str, Any],
        sig: str,
        assignable: bool,
        previous: int | None = None,
    ) -> Tracked:
        state: dict[str, Any] = {
            "sig": sig,
            "line": result["line"],
            "versions": [result["version"]],
            "channel": result["channel"],
            "phase": result["phase"],
            "sha": result["sha"],
            "python": result["python"],
            "failing_tests": result["failing_tests"],
            "failing_total": result["failing_total"],
            "assigned": False,
            "commented": [],
        }
        if previous is not None:
            state["previous"] = previous
        issue = self.api.request(
            "POST",
            "/issues",
            {
                "title": issue_title(state),
                "body": render_body(state, self.run_url, self.workflow_url),
                "labels": [LABEL],
            },
        )
        tracked = Tracked(number=int(issue["number"]), open=True, state=state)
        self.issues.append(tracked)
        print(f"Opened issue #{tracked.number}.")
        if self.copilot_enabled and assignable:
            self.copilot_ops.append({"op": "assign", "issue": tracked.number})
        return tracked

    def _close(self, issue: Tracked, reason: str) -> None:
        self.api.request(
            "PATCH",
            f"/issues/{issue.number}",
            {"state": "closed", "state_reason": reason},
        )
        issue.open = False

    def _comment(self, number: int, body: str) -> None:
        self.api.request("POST", f"/issues/{number}/comments", {"body": body})


def run_copilot_ops(
    ops: list[dict[str, Any]],
    copilot_api: Any,
    issues_api: Any,
    repo: str,
    base_branch: str,
    run_url: str,
    workflow_url: str,
) -> int:
    """Run queued Copilot operations and return how many failed.

    Only ``copilot_api`` uses the Copilot token. Issue state is reread and
    rewritten with ``issues_api`` so a failed operation retries on the next run.
    """
    failures = 0
    for op in ops:
        number = op.get("issue")
        if not isinstance(number, int) or op.get("op") not in ("assign", "comment"):
            continue
        try:
            issue = issues_api.request("GET", f"/issues/{number}")
            state = parse_marker(issue.get("body"))
            if state is None or issue.get("state") != "open":
                continue
            assignees = {a.get("login", "") for a in issue.get("assignees") or []}
            if op["op"] == "assign":
                if state["assigned"]:
                    continue
                if not assignees & COPILOT_LOGINS:
                    response = copilot_api.request(
                        "POST",
                        f"/issues/{number}/assignees",
                        {
                            "assignees": [COPILOT_ASSIGNEE],
                            "agent_assignment": {
                                "target_repo": repo,
                                "base_branch": base_branch,
                                "custom_instructions": COPILOT_INSTRUCTIONS,
                            },
                        },
                    )
                    # GitHub silently ignores assignees the caller cannot assign.
                    assigned = {
                        a.get("login", "")
                        for a in (response or {}).get("assignees") or []
                    }
                    if not assigned & COPILOT_LOGINS:
                        raise GitHubError(422)
                state["assigned"] = True
                # Copilot reads the versions already listed; mention only later ones.
                state["commented"] = list(state["versions"])
                print(f"Assigned Copilot to issue #{number}.")
            else:
                version, pr = op.get("version"), op.get("pr")
                if (
                    not isinstance(version, str)
                    or parse_version(version) is None
                    or not isinstance(pr, int)
                    or version in state["commented"]
                ):
                    continue
                copilot_api.request(
                    "POST",
                    f"/issues/{pr}/comments",
                    {
                        "body": f"@copilot Home Assistant `{version}` also fails with "
                        f"the same tests ({run_url}). Make sure the fix also passes "
                        f"on `{version}`. Details are in #{number}."
                    },
                )
                state["commented"].append(version)
                print(f"Asked Copilot to cover {version} on pull request #{pr}.")
            issues_api.request(
                "PATCH",
                f"/issues/{number}",
                {"body": render_body(state, run_url, workflow_url)},
            )
        except (GitHubError, urllib.error.URLError, TimeoutError) as err:
            failures += 1
            status = getattr(err, "status", "network")
            print(
                f"::warning::Copilot operation for issue #{number} failed ({status})."
            )
    return failures


# Install


def _pip(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "pip", "install", *args], check=True)


def _dev_requirements_without_ha() -> list[str]:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]
    return [
        requirement
        for requirement in project["optional-dependencies"]["dev"]
        if re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0].lower()
        != "homeassistant"
    ]


CONTRACT_SNIPPET = """
import json, pathlib, sys
import homeassistant
components = pathlib.Path(homeassistant.__file__).parent / "components"
print(json.dumps(sorted({
    requirement
    for name in sys.argv[1:]
    for requirement in json.loads(
        (components / name / "manifest.json").read_text()
    ).get("requirements", [])
})))
"""


def install(mode: str, version: str) -> None:
    """Install HA and test dependencies.

    ``supported`` matches the CI matrix install. ``watch`` installs the requested
    HA version even outside the dev extra's range, then checks dependency
    consistency, and retries once to absorb transient feed errors.
    """
    if parse_version(version) is None:
        raise SystemExit(
            "Home Assistant version must look like 2026.12.0 or 2026.12.0b1"
        )
    attempts = 2 if mode == "watch" else 1
    for attempt in range(1, attempts + 1):
        try:
            if mode == "supported":
                _pip("-e", f"{REPO_ROOT}[dev]", f"homeassistant=={version}")
            else:
                _pip(f"homeassistant=={version}", *_dev_requirements_without_ha())
                _pip("--no-deps", "-e", str(REPO_ROOT))
            requirements = json.loads(
                subprocess.run(
                    [sys.executable, "-c", CONTRACT_SNIPPET, *CONTRACT_COMPONENTS],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )
            if requirements:
                _pip(*requirements)
            if mode == "watch":
                subprocess.run([sys.executable, "-m", "pip", "check"], check=True)
            break
        except subprocess.CalledProcessError:
            if attempt == attempts:
                raise SystemExit("Dependency install failed.") from None
            print("::warning::Dependency install failed; retrying once.")
            time.sleep(30)
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from importlib.metadata import version as v; "
            "print('Python', sys.version.split()[0]); "
            "print('Home Assistant', v('homeassistant')); "
            "print('github-copilot-sdk', v('github-copilot-sdk'))",
        ],
        check=True,
    )


# CLI


def _write_output(path: str | None, name: str, value: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as output:
            output.write(f"{name}={value}\n")
    print(f"{name}={value}")


def _repo_context() -> tuple[str, str, str]:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ["GITHUB_REPOSITORY"]
    run_url = f"{server}/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    workflow_url = f"{server}/{repo}/actions/workflows/ha-release-watch.yml"
    return repo, run_url, workflow_url


def cmd_discover(args: argparse.Namespace) -> None:
    releases = _fetch_json(urllib.request.Request(PYPI_URL))["releases"]
    try:
        targets = select_targets(releases, args.version or None)
    except ValueError as err:
        raise SystemExit(str(err)) from None
    retest: set[str] = set()
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if token and repo:
        retest = retest_versions(load_issues(GitHub(token, repo)))
    for target in targets:
        target["retest"] = args.force or target["version"] in retest
    print(f"Selected {len(targets)} Home Assistant release(s).")
    _write_output(args.output, "matrix", json.dumps(targets, separators=(",", ":")))


def cmd_result(args: argparse.Namespace) -> None:
    tests = failing_tests(Path(args.junit)) if args.junit else []
    phase = None
    if args.phase_file and Path(args.phase_file).is_file():
        phase = Path(args.phase_file).read_text().strip() or None
    data = {
        "version": args.version,
        "sha": args.sha,
        "python": args.python,
        "outcome": args.outcome,
        "phase": phase if args.outcome == "failure" else None,
        "sig": signature(phase, tests) if args.outcome == "failure" else None,
        "failing_tests": tests[:MAX_FAILING_TESTS],
        "failing_total": len(tests),
    }
    result = validate_result(data)
    if result is None:
        raise SystemExit("Result data failed validation.")
    Path(args.out).write_text(json.dumps(result))
    print(f"Recorded {args.outcome} for {args.version}.")


def cmd_report(args: argparse.Namespace) -> None:
    repo, run_url, workflow_url = _repo_context()
    api = GitHub(os.environ["GITHUB_TOKEN"], repo)
    results = []
    for path in sorted(Path(args.results).glob("*.json")):
        result = None
        if path.stat().st_size <= MAX_RESULT_BYTES:
            try:
                result = validate_result(json.loads(path.read_text()))
            except ValueError:
                result = None
        if result is None:
            print("::warning::Ignored an invalid result artifact.")
            continue
        results.append(result)
    results.sort(key=lambda r: parse_version(r["version"]))
    print(f"Loaded {len(results)} result(s).")

    reporter = Reporter(
        api, run_url, workflow_url, load_issues(api), copilot_enabled=args.copilot
    )
    if any(r["outcome"] == "failure" for r in results):
        reporter.ensure_label()
    for result in results:
        reporter.process(result)
    Path(args.plan_out).write_text(json.dumps(reporter.copilot_ops))
    print(f"Queued {len(reporter.copilot_ops)} Copilot operation(s).")


def cmd_copilot(args: argparse.Namespace) -> None:
    repo, run_url, workflow_url = _repo_context()
    ops = json.loads(Path(args.plan).read_text())
    if not isinstance(ops, list):
        raise SystemExit("Invalid Copilot plan.")
    if not ops:
        return
    token = os.environ.get("COPILOT_ASSIGN_TOKEN")
    if not token:
        print("::warning::COPILOT_ASSIGN_TOKEN is not set; skipping Copilot.")
        return
    failures = run_copilot_ops(
        [op for op in ops if isinstance(op, dict)],
        GitHub(token, repo),
        GitHub(os.environ["GITHUB_TOKEN"], repo),
        repo,
        args.base_branch,
        run_url,
        workflow_url,
    )
    if failures:
        print(
            f"::warning::{failures} Copilot operation(s) failed; they retry next run."
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("install", help="install HA and test dependencies")
    p.add_argument("--mode", choices=("supported", "watch"), default="supported")
    p.add_argument("version")
    p.set_defaults(func=lambda a: install(a.mode, a.version))

    p = sub.add_parser("discover", help="select HA releases to test")
    p.add_argument("--version", default="")
    p.add_argument("--force", action="store_true")
    p.add_argument("--output", default=os.environ.get("GITHUB_OUTPUT"))
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("result", help="write a test result artifact")
    for name in ("--version", "--sha", "--python", "--outcome", "--out"):
        p.add_argument(name, required=True)
    p.add_argument("--phase-file")
    p.add_argument("--junit")
    p.set_defaults(func=cmd_result)

    p = sub.add_parser("report", help="update ha-compat issues")
    p.add_argument("--results", required=True)
    p.add_argument("--plan-out", required=True)
    p.add_argument("--copilot", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("copilot", help="run queued Copilot operations")
    p.add_argument("--plan", required=True)
    p.add_argument("--base-branch", default="main")
    p.set_defaults(func=cmd_copilot)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
