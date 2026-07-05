# Zero-config entry points. `make setup && make demo` works on a fresh clone
# with no API key (auto-falls back to offline scripted mode).

PY := .venv/bin/python

setup:            ## create venv + install deps (uses uv if installed, else pip)
	@if command -v uv >/dev/null 2>&1; then \
		uv venv -q && uv pip install -q -r requirements.txt; \
	else \
		python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; \
	fi
	@echo "✓ ready — try: make demo"

demo:             ## run Eleanor's case end-to-end in the terminal
	$(PY) demo.py

demo-hard:        ## adversarial scenario: every supplier fails, case escalates
	$(PY) demo.py --hard

dashboard:        ## live dashboard at http://localhost:8000
	.venv/bin/uvicorn app.main:app --port 8000

test:             ## zero-LLM end-to-end tests (happy + hard mode)
	$(PY) tests/test_flow.py

evals:            ## extraction-accuracy evals (needs an LLM key in .env)
	$(PY) evals/run_evals.py --n 10

help:
	@grep -E '^[a-z-]+:.*##' Makefile | sed 's/:.*##/ —/'

.PHONY: setup demo demo-hard dashboard test evals help
