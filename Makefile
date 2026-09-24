PYTHON ?= .venv/bin/python
.PHONY: install install-dev test lint type-check serve status audit
install:
	python3.11 -m pip install -e .
install-dev:
	python3.11 -m pip install -e '.[dev]'
test:
	$(PYTHON) -m pytest tests_v1 -q
lint:
	$(PYTHON) -m ruff check free_router tests_v1 main.py server/app.py server/__main__.py
	$(PYTHON) -m ruff format --check free_router tests_v1 main.py server/app.py server/__main__.py
type-check:
	$(PYTHON) -m mypy free_router
serve:
	$(PYTHON) -m free_router.cli serve
status:
	$(PYTHON) -m free_router.cli status
audit:
	$(PYTHON) -m free_router.cli audit
