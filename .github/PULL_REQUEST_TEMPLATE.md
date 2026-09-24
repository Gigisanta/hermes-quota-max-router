## What does this PR do?

A 1-2 sentence summary. Reference the issue it closes with `Closes #NNN`.

## Why?

Link to the issue or describe the user pain. If it's a new feature, link the
discussion where it was designed.

## How was it tested?

- [ ] `make lint type-check test` passes locally
- [ ] Added new tests (file + names)
- [ ] If a provider was admitted, recorded account quota, price, access and editorial evaluation

## Checklist

- [ ] Code follows the existing style (ruff + mypy clean)
- [ ] No new `print()` or `sys.path` hacks
- [ ] Queue and privacy behavior remain fail-closed
- [ ] No paid, local GPU, or simulated fallback was introduced
- [ ] Updated `README.md` and `CHANGELOG.md` when behavior changed
- [ ] Self-reviewed the diff before requesting review
