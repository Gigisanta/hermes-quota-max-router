.PHONY: install install-dev test test-cov lint format type-check serve dashboard clean

install:
	pip install -r requirements.txt

install-dev:
	pip install -r requirements.txt -r requirements-dev.txt
	pre-commit install

test:
	pytest tests/ -q

test-cov:
	pytest tests/ --cov=core --cov=server --cov=dashboard --cov-report=term-missing --cov-report=xml -q

lint:
	ruff check core/ server/ scripts/ dashboard/ tests/ examples/
	ruff format --check core/ server/ scripts/ dashboard/ tests/ examples/

format:
	ruff check core/ server/ scripts/ dashboard/ tests/ examples/ --fix
	ruff format core/ server/ scripts/ dashboard/ tests/ examples/

type-check:
	mypy --ignore-missing-imports --no-strict-optional core/ server/

serve:
	python scripts/run_router_live.py

dashboard:
	python -m dashboard.app

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache coverage.xml .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} \;
