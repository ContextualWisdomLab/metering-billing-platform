# Validation dumps

Operator and contributor validation for this repository. The customer-facing start page is [README.md](../../README.md). Policy and successor-PR order live in [CONTRIBUTING.md](../../CONTRIBUTING.md).

## Quality dependency

```bash
python3 -m pip install --only-binary=:all: --require-hashes -r requirements-quality.txt
```

## Unit suite

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

## Coverage

```bash
python3 -m coverage run --branch -m unittest discover -s tests -p 'test_*.py'
python3 -m coverage report --fail-under=100 --show-missing
```

Repository tooling must report 100% statement and branch coverage. The CI job sources `scripts` only:

```bash
python3 -m coverage run --branch --source=scripts -m unittest discover -s tests -v
python3 -m coverage report --fail-under=100 --show-missing
```

## Repository contracts

```bash
python3 scripts/validate_repository.py
```

or, from CI:

```bash
python3 scripts/validate_repository.py .
```

A valid tree prints `repository contracts valid:` and the resolved root. Failures are one diagnostic per line: missing required files, schema identity errors, SQL naming violations, unresolved placeholders, or mutable GitHub Action references.

## Compile check

```bash
python3 -m compileall -q scripts tests
```

## Exact-head CI reminder

Checkout must use the pull-request or push head. GitHub Actions must be commit-pinned. See [CONTRIBUTING.md](../../CONTRIBUTING.md#exact-head-ci).
