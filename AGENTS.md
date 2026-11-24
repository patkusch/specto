# Working in specto

Watches a recorded expert walkthrough of a system and writes down what the system must do

## Run the tests

You need Python 3.11 or newer.

```bash
pip install -e ".[dev]"
python -m pytest -q
```

Every test should pass before you start and after you finish.

## Where things are

- `tests/`: the tests
- `examples/`: worked examples
- `docs/`: written documentation
- `scripts/`: helper scripts, not part of the tool itself

## Before you commit

- Make sure the tests pass.
- Put changes people will notice in `CHANGELOG.md`.
- Do not put real passwords, keys or private data in test files or examples. Use made-up ones.
