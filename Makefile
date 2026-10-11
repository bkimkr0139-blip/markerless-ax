PY := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: setup test core-test api devlog clean

setup:
	python3 -m venv .venv
	$(PIP) install -U pip
	$(PIP) install -e spatial-core -e backend
	$(PIP) install pytest httpx

test: core-test backend-test

core-test:
	cd spatial-core && ../.venv/bin/python -m pytest tests -q

backend-test:
	cd backend && ../.venv/bin/python -m pytest tests -q

api:
	cd backend && MARKERLESS_DEBUG=1 MARKERLESS_API_KEY="$$(cat ../.api-key 2>/dev/null)" ../.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8111 --reload --timeout-graceful-shutdown 2

devlog:
	tail -n 50 -f backend/logs/dev.log

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache spatial-core/.pytest_cache backend/.pytest_cache
