PY      ?= python3
PORT    ?= 8777
BIND    ?= 127.0.0.1
VENV    ?= .venv
LATENCY ?= 5
QUICK   ?= 600
HOLD    ?= 3600
DURATION?= 0
DB      ?= data/droptrace.db

# The venv is optional. Not every machine can build one (Ubuntu without
# python3-venv has no ensurepip), and the dependencies are often already
# installed system-wide, so use the venv only when it can actually import the
# app and fall back to the plain interpreter otherwise.
VENVPY  = $(VENV)/bin/python
RUNPY   = $(shell [ -x $(VENVPY) ] && $(VENVPY) -c "import fastapi" >/dev/null 2>&1 \
            && echo $(VENVPY) || echo $(PY))

.PHONY: help venv install serve open run probe burst trace outages report test test-net check-frontend clean distclean

help:
	@echo "DropTrace — continuous drop monitor"
	@echo
	@echo "  make install         create .venv (reusing system packages) and check deps"
	@echo "  make serve           dashboard on http://$(BIND):$(PORT)/  (Ctrl+C to stop)"
	@echo "  make open            same, but also opens the dashboard in your browser"
	@echo "  make run             headless sampling in the terminal"
	@echo "  make probe           one probe round now, then exit"
	@echo "  make burst           measure loss now with a counted handshake burst"
	@echo "  make trace           trace the path now and keep the hop list"
	@echo "  make outages         list every recorded outage"
	@echo "  make report          aggregates: uptime, latency, throughput, downtime"
	@echo "  make test            run the test suite (no internet needed)"
	@echo "  make check-frontend  render the dashboard headlessly against a live server"
	@echo "  make clean           remove caches; distclean also removes db + venv"
	@echo
	@echo "  On Windows just double-click start.bat; inside WSL/Linux run ./start.sh"
	@echo "  tune with: make serve LATENCY=1 QUICK=900 HOLD=1800 DURATION=8h"
	@echo "  drops are caught by the probe cadence; DURATION=0 means run until stopped"

venv:
	@$(RUNPY) -c "import fastapi, uvicorn, httpx, aiosqlite" 2>/dev/null || { \
		echo "  Python dependencies are missing."; \
		echo "  Build a venv:  sudo apt install python3-venv && make install"; \
		echo "  or install:    $(PY) -m pip install --user -r requirements.txt"; \
		exit 1; }

install:
	@test -x $(VENVPY) || $(PY) -m venv --system-site-packages $(VENV) || true
	@$(VENVPY) -m pip --version >/dev/null 2>&1 || { \
		echo "  $(PY) cannot create a venv with pip (no ensurepip)."; \
		echo "  Install python3-venv (sudo apt install python3-venv) and retry."; \
		exit 1; }
	@$(VENVPY) -m pip install -r requirements.txt
	@echo "dependencies ready in $(VENV)"

serve: venv
	$(RUNPY) -m droptrace serve --bind $(BIND) --web-port $(PORT) \
		--latency-interval $(LATENCY) --quick-interval $(QUICK) --sustained-interval $(HOLD) --duration $(DURATION) --db $(DB)

open: venv
	$(RUNPY) -m droptrace serve --bind $(BIND) --web-port $(PORT) \
		--latency-interval $(LATENCY) --quick-interval $(QUICK) --sustained-interval $(HOLD) --duration $(DURATION) --db $(DB) --open

run: venv
	$(RUNPY) -m droptrace run --latency-interval $(LATENCY) \
		--quick-interval $(QUICK) --sustained-interval $(HOLD) --duration $(DURATION) --db $(DB)

probe: venv
	$(RUNPY) -m droptrace probe --db $(DB)

burst: venv
	$(RUNPY) -m droptrace burst --db $(DB) --no-save

trace: venv
	$(RUNPY) -m droptrace trace --db $(DB)

outages: venv
	$(RUNPY) -m droptrace outages --db $(DB) --window 24h

report: venv
	$(RUNPY) -m droptrace report --db $(DB) --window all

test: venv
	$(RUNPY) -m pytest -q

test-net: venv
	DROPTRACE_NET_TESTS=1 $(RUNPY) -m pytest -q -m net

check-frontend: venv
	@$(RUNPY) -c "import urllib.request;urllib.request.urlopen('http://$(BIND):$(PORT)/api/health',timeout=3)" 2>/dev/null || \
		{ echo "no server on http://$(BIND):$(PORT) — run 'make serve' first"; exit 1; }
	node scripts/check_frontend.mjs http://$(BIND):$(PORT)

clean:
	@find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .pytest_cache

distclean: clean
	@rm -rf $(VENV) data tmp-ui
