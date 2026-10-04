"""Foundation CI uses Linux self-hosted runners without dropping test gates."""

import io
import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


def _without_comment(line: str) -> str:
    """Strip YAML comments outside single/double quoted scalar tokens."""
    quote = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
        elif quote == '"' and char == "\\":
            escaped = True
        elif char == quote:
            quote = None
        elif quote is None and char in "\"'":
            quote = char
        elif quote is None and char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index].rstrip()
    return line.rstrip()


def _scalar(token: str) -> str:
    """Support plain strings, JSON double quotes, and YAML single quotes."""
    token = token.strip()
    if token.startswith('"'):
        value = json.loads(token)
        if not isinstance(value, str):
            raise ValueError("Expected string scalar")
        return value
    if token.startswith("'"):
        if not re.fullmatch(r"'(?:[^']|'')*'", token):
            raise ValueError("Malformed single-quoted scalar")
        return token[1:-1].replace("''", "'")
    if not token or any(char in token for char in "[]{}&*!|>\"'"):
        raise ValueError("Unsupported scalar syntax")
    return token


def _runner_mapping(workflow: str) -> dict[str, object]:
    """Read only jobs/repository-contracts/runs-on in a YAML block subset.

    Supported: space-indented block mappings, plain/quoted string keys and
    scalars, comments, and an inline comma-separated string labels list.
    Double quotes use JSON escapes, not general YAML escapes. Single quotes
    allow doubled quotes. Selected keys cannot contain colons, and labels
    cannot contain commas. No anchors, aliases, flow mappings, tabs, document
    separators, or block/list-valued runner fields. Root, jobs, and selected
    job direct siblings must be mapping entries; nested unrelated bodies are
    opaque. This is not a general YAML parser or whole-workflow validator.
    Duplicate selected path keys/runner fields and unsupported forms fail.
    """
    lines = []
    for raw in workflow.splitlines():
        line = _without_comment(raw)
        if not line.strip():
            continue
        prefix = line[:len(line) - len(line.lstrip())]
        if "\t" in prefix:
            raise ValueError("Tab indentation is unsupported")
        if line.strip() in {"---", "..."}:
            raise ValueError("Document markers are unsupported")
        lines.append((len(prefix), line.strip()))
    if not lines or lines[0][0] != 0:
        raise ValueError("Expected root mapping at column zero")
    parent_indent = -1
    for key in ("jobs", "repository-contracts", "runs-on"):
        child_indent = min((indent for indent, _ in lines), default=-1)
        if child_indent <= parent_indent:
            raise ValueError("Expected nested block mapping")
        matches = []
        for index, (indent, text) in enumerate(lines):
            if indent != child_indent:
                continue
            if re.match(r"-\s", text):
                raise ValueError("Sequence entries are unsupported in enclosing mappings")
            name, separator, value = text.partition(":")
            if not separator:
                raise ValueError("Expected mapping entry, not document marker or prose")
            name = _scalar(name)
            if name == key:
                matches.append((index, value.strip()))
        if len(matches) != 1 or matches[0][1]:
            raise ValueError(f"Expected one block mapping at {key}")
        index = matches[0][0]
        parent_indent = child_indent
        end = index + 1
        while end < len(lines) and lines[end][0] > parent_indent:
            end += 1
        lines = lines[index + 1:end]
    if not lines:
        raise ValueError("Empty runs-on mapping")
    child_indent = min(indent for indent, _ in lines)
    result = {}
    for indent, text in lines:
        if indent != child_indent:
            raise ValueError("Nested runner fields are unsupported")
        name, separator, value = text.partition(":")
        name = _scalar(name)
        if not separator or name in result:
            raise ValueError("Invalid or duplicate runner field")
        value = value.strip()
        if name == "labels":
            if not (value.startswith("[") and value.endswith("]")):
                raise ValueError("Expected inline labels list")
            result[name] = [_scalar(token) for token in value[1:-1].split(",")]
        else:
            result[name] = _scalar(value)
    return result


class FoundationRunnerTests(unittest.TestCase):
    """Prevent hosted billing failures from stopping repository acceptance."""

    def test_foundation_uses_existing_linux_runner_and_preserves_all_gates(self) -> None:
        """Runner migration must keep PostgreSQL and complete coverage enabled."""
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/ci.yml").read_text()
        try:
            runner = _runner_mapping(workflow)
        except ValueError as error:
            self.fail(f"Unsupported repository-contracts runner mapping: {error}")
        self.assertEqual(runner, {
            "group": "CWL CI isolated",
            "labels": ["self-hosted", "Linux", "X64", "cwlab-ci-isolated"],
        })
        self.assertIn("image: postgres:18", workflow)
        self.assertIn("python -m coverage run --branch --source=scripts,metering_billing", workflow)
        self.assertIn("python -m coverage report --fail-under=100 --show-missing", workflow)
        self.assertIn("python scripts/validate_repository.py .", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("--require-hashes", workflow)


WORKFLOW_FIXTURE = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
RUNNER_FIXTURE = "    runs-on:\n      group: CWL CI isolated\n      labels: [self-hosted, Linux, X64, cwlab-ci-isolated]"
EXPECTED_RUNNER = {"group": "CWL CI isolated", "labels": ["self-hosted", "Linux", "X64", "cwlab-ci-isolated"]}


class OracleRegressionTests(unittest.TestCase):
    """Exercise the actual workflow checker, not a separate parser oracle."""

    def outcome(self, workflow):
        """Return one real checker result using only an in-memory file read."""
        case = FoundationRunnerTests("test_foundation_uses_existing_linux_runner_and_preserves_all_gates")
        with patch.object(Path, "read_text", return_value=workflow):
            result = unittest.TextTestRunner(stream=io.StringIO()).run(case)
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.skipped, [])
        return result

    def negatives(self):
        """Return otherwise-valid runner substitutions and structural denials."""
        return {
            "comment_decoy": WORKFLOW_FIXTURE.replace(RUNNER_FIXTURE, "    runs-on: privileged\n    # group: CWL CI isolated\n    # labels: [self-hosted, Linux, X64, cwlab-ci-isolated]"),
            "unrelated_job": WORKFLOW_FIXTURE.replace(RUNNER_FIXTURE, "    runs-on: privileged") + "\n  another-job:\n" + RUNNER_FIXTURE + "\n",
            "wrong_field": WORKFLOW_FIXTURE.replace("    runs-on:", "    wrong-field:"),
            "wrong_group": WORKFLOW_FIXTURE.replace("group: CWL CI isolated", "group: privileged"),
            "wrong_labels": WORKFLOW_FIXTURE.replace("cwlab-ci-isolated]", "privileged]"),
            "missing_jobs": WORKFLOW_FIXTURE.replace("jobs:", "not-jobs:", 1),
            "empty_jobs": WORKFLOW_FIXTURE[:WORKFLOW_FIXTURE.index("jobs:")] + "jobs:\n",
            "missing_job": WORKFLOW_FIXTURE.replace("repository-contracts:", "not-contracts:", 1),
            "empty_job": WORKFLOW_FIXTURE[:WORKFLOW_FIXTURE.index("  repository-contracts:")] + "  repository-contracts:\n",
            "missing_runner": WORKFLOW_FIXTURE.replace(RUNNER_FIXTURE, ""),
            "empty_runner": WORKFLOW_FIXTURE.replace(RUNNER_FIXTURE, "    runs-on:"),
            "duplicate_jobs": WORKFLOW_FIXTURE + "\njobs:\n  other:\n    runs-on: privileged\n",
            "duplicate_job": WORKFLOW_FIXTURE + "\n  repository-contracts:\n" + RUNNER_FIXTURE + "\n",
            "duplicate_runner": WORKFLOW_FIXTURE.replace(RUNNER_FIXTURE, RUNNER_FIXTURE + "\n" + RUNNER_FIXTURE),
            "duplicate_group": WORKFLOW_FIXTURE.replace("      group: CWL CI isolated", "      group: privileged\n      group: CWL CI isolated"),
            "duplicate_labels": WORKFLOW_FIXTURE.replace("      labels:", "      labels: [privileged]\n      labels:"),
            "extra_field": WORKFLOW_FIXTURE.replace("      group:", "      extra: privileged\n      group:"),
            "nested_runner": WORKFLOW_FIXTURE.replace("      labels:", "        labels:"),
            "malformed_root": "malformed missing colon\n" + WORKFLOW_FIXTURE,
            "malformed_jobs": WORKFLOW_FIXTURE.replace("  repository-contracts:", "  malformed missing colon\n  repository-contracts:"),
            "malformed_job": WORKFLOW_FIXTURE.replace("    name: Repository contracts", "    malformed missing colon"),
            "root_sequence_sibling": "- malformed: value\n" + WORKFLOW_FIXTURE,
            "jobs_sequence_sibling": WORKFLOW_FIXTURE.replace("  repository-contracts:", "  - malformed: value\n  repository-contracts:"),
            "job_sequence_sibling": WORKFLOW_FIXTURE.replace("    name: Repository contracts", "    - malformed: value\n    name: Repository contracts"),
            "multiple_documents": WORKFLOW_FIXTURE + "\n---\nname: Second\n",
            "document_end": WORKFLOW_FIXTURE + "\n...\n",
            "root_indent": " " + WORKFLOW_FIXTURE,
            "tab_indent": WORKFLOW_FIXTURE.replace("      group:", "\tgroup:"),
            "alias": WORKFLOW_FIXTURE.replace("group: CWL CI isolated", "group: *isolated"),
            "anchor": WORKFLOW_FIXTURE.replace("    runs-on:", "    runs-on: &isolated"),
            "flow_mapping": WORKFLOW_FIXTURE.replace(RUNNER_FIXTURE, "    runs-on: {group: CWL CI isolated, labels: [self-hosted, Linux, X64, cwlab-ci-isolated]}"),
            "block_scalar": WORKFLOW_FIXTURE.replace("group: CWL CI isolated", "group: |\n        CWL CI isolated"),
            "block_labels": WORKFLOW_FIXTURE.replace("      labels: [self-hosted, Linux, X64, cwlab-ci-isolated]", "      labels:\n        - self-hosted\n        - Linux\n        - X64\n        - cwlab-ci-isolated"),
            "single_unterminated": WORKFLOW_FIXTURE.replace("group: CWL CI isolated", "group: 'CWL CI isolated"),
            "single_trailing": WORKFLOW_FIXTURE.replace("group: CWL CI isolated", "group: 'CWL CI isolated' tail'"),
            "double_unterminated": WORKFLOW_FIXTURE.replace("group: CWL CI isolated", 'group: "CWL CI isolated'),
            "double_trailing": WORKFLOW_FIXTURE.replace("group: CWL CI isolated", 'group: "CWL CI isolated" tail'),
            "yaml_only_escape": WORKFLOW_FIXTURE.replace("group: CWL CI isolated", 'group: "CWL CI isolated\\ "'),
            "colon_key": WORKFLOW_FIXTURE.replace("      group:", "      'group:other':"),
            "comma_label": WORKFLOW_FIXTURE.replace("cwlab-ci-isolated]", "'cwlab-ci-isolated,other']"),
        }

    def test_supported_positive_forms(self):
        """Allow the exact workflow and declared quotes/comments/spacing."""
        quoted = WORKFLOW_FIXTURE.replace("jobs:", '"jobs": # root').replace("  repository-contracts:", "  'repository-contracts': # job").replace(RUNNER_FIXTURE, "    'runs-on': # mapping\n      \"group\": \"CWL CI isolated\" # group\n      'labels': [ 'self-hosted' , \"Linux\" , X64 , 'cwlab-ci-isolated' ]")
        escaped = WORKFLOW_FIXTURE.replace("group: CWL CI isolated", 'group: "CWL\\u0020CI isolated"')
        for name, workflow in {"current": WORKFLOW_FIXTURE, "quoted": quoted, "json_escape": escaped}.items():
            with self.subTest(name=name):
                self.assertEqual(self.outcome(workflow).failures, [])

    def test_every_negative_reaches_one_assertion_failure(self):
        """Reject each counterexample with one checker failure, not errors."""
        cases = self.negatives()
        self.assertGreaterEqual(len(cases), 30)
        for name, workflow in cases.items():
            with self.subTest(name=name):
                self.assertEqual(len(self.outcome(workflow).failures), 1)

    def test_selected_runner_bypass_is_detected(self):
        """An oracle bypass must not masquerade as a negative-control PASS."""
        case = OracleRegressionTests("test_every_negative_reaches_one_assertion_failure")
        with patch.object(sys.modules[__name__], "_runner_mapping", return_value=EXPECTED_RUNNER):
            result = unittest.TextTestRunner(stream=io.StringIO()).run(case)
        self.assertEqual(result.testsRun, 1)
        self.assertGreater(len(result.failures), 0)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.skipped, [])

    def test_unexpected_checker_error_is_not_a_valid_denial(self):
        """The control harness fails if the checker raises an unrelated error."""
        with patch.object(sys.modules[__name__], "_runner_mapping", side_effect=RuntimeError("fixture")):
            with self.assertRaises(AssertionError):
                self.outcome(WORKFLOW_FIXTURE)

    def test_scalar_quote_boundaries(self):
        """Declare single-quote doubling and JSON escapes without general YAML."""
        self.assertEqual(_scalar("'can''t'"), "can't")
        self.assertEqual(_scalar('"a\\\"b"'), 'a"b')
        self.assertEqual(_scalar('"a\\u0020b"'), "a b")
        for token in ["'a'b'", "'a", '"a"tail', '"a\\ "']:
            with self.subTest(token=token):
                with self.assertRaises(ValueError):
                    _scalar(token)


if __name__ == "__main__":
    unittest.main()
