# Operator workflow wrapper for nodes.quip.network.
#
# Two main entry points:
#   make testnet   -> join the live Quip Testnet
#   make localdev  -> self-contained single-validator dev chain
#
# Run `make help` for the full target list.

PROFILE          ?= cpu
SUDO_KEY         ?= //Alice
DATA             ?= data
# Plain `docker compose ...` defaults to live Quip Testnet — only
# docker-compose.yml is auto-loaded now that the localdev override was
# renamed off the magic `docker-compose.override.yml` filename.
COMPOSE          := docker compose
# Opt-in stack with the dev-chain override layered on top.
COMPOSE_LOCALDEV := docker compose -f docker-compose.yml -f docker-compose.localdev.yml

.DEFAULT_GOAL := help

.PHONY: help testnet localdev pull down logs clean clean-chain require-env updateconfig updateconfig-docker

help:
	@echo "nodes.quip.network — operator targets"
	@echo ""
	@echo "  make testnet           Pull + bring up stack against live Quip Testnet"
	@echo "  make localdev          Wipe chain, bring up self-contained dev stack"
	@echo "                         (validator on --chain=dev, faucet, seeded topology,"
	@echo "                          registered miner, dashboard, caddy)"
	@echo "  make updateconfig      Convert a v0.1 data/config.toml to the v0.2 [miner]"
	@echo "                         schema (backs up originals to data/.v0.1_backup/)."
	@echo "                         Override the dir with DATA=/path/to/data."
	@echo "  make updateconfig-docker  Same as above but inside python:3.12-alpine"
	@echo "                            (use when the host has Python < 3.11)."
	@echo "  make pull              Pull images for PROFILE"
	@echo "  make down              Tear down both profile sets"
	@echo "  make logs              Tail validator + miner logs"
	@echo "  make clean-chain       Wipe data/validator-data/chains"
	@echo "  make clean             Full reset: down + wipe chain, pgdata volume, dashboard-data"
	@echo ""
	@echo "Variables (override on cmdline):"
	@echo "  PROFILE=$(PROFILE)         compose profile (cpu | cuda; faucet layers additively)"
	@echo "  SUDO_KEY=$(SUDO_KEY)       dev URI used for localdev seeding"
	@echo "  DATA=$(DATA)               data dir converted by updateconfig"

require-env:
	@test -f .env || { \
	    echo "error: .env not found. Create it with:"; \
	    echo "    cp env.example .env"; \
	    echo "    printf 'PUID=%s\\nPGID=%s\\n' \"\$$(id -u)\" \"\$$(id -g)\" >> .env"; \
	    exit 1; \
	}

# Live Quip Testnet. Plain `docker compose` is testnet now (the localdev
# override is opt-in via -f docker-compose.localdev.yml, not auto-loaded).
testnet: require-env
	$(COMPOSE) --profile $(PROFILE) pull
	$(COMPOSE) --profile $(PROFILE) up -d
	@echo ""
	@echo "testnet stack up. tail logs: make logs"

# Self-contained dev chain. Layers docker-compose.localdev.yml on top of the
# base docker-compose.yml to flip the validator to --chain=dev and pull
# quip-faucet into the cpu/cuda profiles. Order matters: validator+faucet
# must produce blocks before sudo seeding; seeding must complete before the
# miner bootstraps (so DefaultTopology + the difficulty are live when the
# miner queries them).
localdev: require-env down clean-chain
	$(COMPOSE_LOCALDEV) --profile $(PROFILE) pull
	$(COMPOSE_LOCALDEV) --profile $(PROFILE) up -d quip-validator quip-faucet
	@echo "waiting for validator to produce blocks..."
	@sleep 12
	$(COMPOSE_LOCALDEV) --profile $(PROFILE) run --rm \
	    -v "$(CURDIR)/scripts/seed-advantage2-topology.py:/seed.py:ro" \
	    --entrypoint python3 cpu /seed.py --sudo-key $(SUDO_KEY)
	$(COMPOSE_LOCALDEV) --profile $(PROFILE) run --rm \
	    --entrypoint quip-miner cpu \
	    bootstrap --validator ws://quip-validator:9944 \
	    --signer-key /data/keystore.json \
	    --faucet-url http://quip-faucet:8087
	$(COMPOSE_LOCALDEV) --profile $(PROFILE) up -d
	@echo ""
	@echo "localdev stack up. tail logs: make logs"
	@echo ""
	@echo "  dashboard            : http://localhost:20049/"
	@echo "  miner REST (v1)      : http://localhost:20049/api/v1/"
	@echo "  faucet (POST)        : http://localhost:20049/api/faucet/request"
	@echo "  substrate RPC        : http://localhost:20049/rpc  (HTTP + WS)"

# v0.1 → v0.2 config converter. Renames [global] → [miner], drops P2P/TLS
# keys that no longer have a consumer (substrate validator owns p2p now),
# and preserves the backend tables ([cpu], [gpu], [cuda.N], [qpu], [dwave],
# ...) verbatim. The original data/ contents are moved into
# data/.v0.1_backup/ so nothing is lost. Idempotent on already-v0.2 dirs.
updateconfig:
	@test -d "$(DATA)" || { echo "error: $(DATA) is not a directory. Override with DATA=path/to/data."; exit 1; }
	python3 scripts/upgrade-config.py "$(DATA)"

# Docker fallback for hosts on Python < 3.11 (e.g. Ubuntu 22.04, which ships
# 3.10 and so doesn't have stdlib `tomllib`). python:3.12-alpine is ~20MB
# and ships tomllib — no pip install needed because the script is stdlib-only.
updateconfig-docker:
	@test -d "$(DATA)" || { echo "error: $(DATA) is not a directory. Override with DATA=path/to/data."; exit 1; }
	docker run --rm \
	    -v "$(abspath $(DATA)):/data" \
	    -v "$(CURDIR)/scripts/upgrade-config.py:/upgrade-config.py:ro" \
	    python:3.12-alpine \
	    python3 /upgrade-config.py /data

pull: require-env
	$(COMPOSE) --profile $(PROFILE) pull

# Use the localdev compose set for down so it reaches the dev-chain
# faucet (which lives in docker-compose.localdev.yml). Harmless on hosts
# that only ran `make testnet` — compose ignores services it can't see.
down:
	$(COMPOSE_LOCALDEV) --profile $(PROFILE) --profile faucet down

logs:
	$(COMPOSE) logs -f --tail=50 quip-validator cpu cuda

# `trash` keeps wiped chains recoverable via macOS Trash per global preference;
# the rm fallback covers Linux/CI hosts without `trash` installed.
clean-chain:
	@if command -v trash >/dev/null 2>&1; then \
	    trash data/validator-data/chains 2>/dev/null || true; \
	else \
	    rm -rf data/validator-data/chains; \
	fi

# Full reset. Tears the stack down, wipes the chain, removes the postgres
# data volume (fixes the cross-project `quip-pgdata` mismatch that breaks
# the dashboard migration with "password authentication failed"), and
# clears dashboard-data so the indexer re-syncs from scratch alongside the
# fresh DB. Destructive — do not run on a production node without a dump.
clean: down clean-chain
	-docker volume rm quip-pgdata 2>/dev/null
	@if command -v trash >/dev/null 2>&1; then \
	    trash dashboard-data 2>/dev/null || true; \
	else \
	    rm -rf dashboard-data; \
	fi
