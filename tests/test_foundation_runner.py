"""Foundation CI uses Linux self-hosted runners without dropping test gates."""

import unittest
from pathlib import Path


class FoundationRunnerTests(unittest.TestCase):
    """Prevent hosted billing failures from stopping repository acceptance."""

    def test_foundation_uses_existing_linux_runner_and_preserves_all_gates(self) -> None:
        """Runner migration must keep PostgreSQL and complete coverage enabled."""
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/ci.yml").read_text()
        self.assertIn("group: CWL CI isolated", workflow)
        self.assertIn("labels: [self-hosted, Linux, X64, cwlab-ci-isolated]", workflow)
        self.assertNotIn("runs-on: ubuntu-latest", workflow)
        self.assertNotIn("runs-on: [self-hosted, Linux, X64, cwlab, ubuntu-latest]", workflow)
        self.assertIn("image: postgres:18", workflow)
        self.assertIn("python -m coverage run --branch --source=scripts,metering_billing", workflow)
        self.assertIn("python -m coverage report --fail-under=100 --show-missing", workflow)
        self.assertIn("python scripts/validate_repository.py .", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("--require-hashes", workflow)


if __name__ == "__main__":
    unittest.main()
