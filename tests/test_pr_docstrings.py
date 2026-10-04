"""Guard function documentation in the ten Python modules changed by PR182.

This explicit scope includes private methods and nested fixture helpers; it does
not claim package-wide docstring coverage or inspect unrelated Python modules.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PR_CHANGED_PYTHON_MODULES = (
    "metering_billing/http_app.py",
    "metering_billing/usage_rating.py",
    "metering_billing/posting_receipt.py",
    "tests/test_ais_outbox_drain.py",
    "tests/test_ais_redirect_denial.py",
    "tests/test_ais_response_bounds.py",
    "tests/test_http_app.py",
    "tests/test_postgres_usage_ledger.py",
    "tests/test_posting_receipt_observation.py",
    "tests/test_usage_rating.py",
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class PrChangedModuleDocstringTests(unittest.TestCase):
    """Check every function definition in the explicitly named PR182 modules."""

    def test_pr_changed_modules_document_all_function_scopes(self) -> None:
        """Reject blank or missing docstrings, including nested and async helpers."""
        for relative_path in PR_CHANGED_PYTHON_MODULES:
            with self.subTest(module=relative_path):
                source = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
                tree = ast.parse(source, filename=relative_path)
                functions = [
                    node for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                self.assertTrue(functions, f"No function definitions in {relative_path}")
                missing = [
                    f"{node.name}:{node.lineno}" for node in functions
                    if not (ast.get_docstring(node) or "").strip()
                ]
                self.assertEqual(missing, [], f"Undocumented function scopes in {relative_path}")


if __name__ == "__main__":
    unittest.main()
