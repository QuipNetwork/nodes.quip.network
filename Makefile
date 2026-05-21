# Operator workflow wrapper for nodes.quip.network.
#
# Two main entry points:
#   make testnet   -> join the live Quip Testnet
#   make localdev  -> self-contained single-validator dev chain
#
# Run `make help` for the full target list.

PROFILE          ?= validator-cpu
SUDO_KEY         ?= //Alice
COMPOSE          := docker compose
COMPOSE_TESTNET  := docker compose -f docker-compose.yml

.DEFAULT_GOAL := help

.PHONY: help testnet localdev pull down logs clean-chain require-env

help:
	@echo "nodes.quip.network — operator targets"
	@echo ""
	@echo "  make testnet           Pull + bring up stack against live Quip Testnet"
	@echo "  make localdev          Wipe chain, bring up self-contained dev stack"
	@echo "                         (validator on --chain=dev, faucet, seeded topology,"
	@echo "                          registered miner, dashboard, caddy)"
	@echo "  make pull              Pull images for PROFILE"
	@echo "  make down              Tear down both profile sets"
	@echo "  make logs              Tail validator + miner logs"
	@echo "  make clean-chain       Wipe data/validator-data/chains"
	@echo ""
	@echo "Variables (override on cmdline):"
	@echo "  PROFILE=$(PROFILE)         compose profile (validator-cpu | validator-cuda)"
	@echo "  SUDO_KEY=$(SUDO_KEY)       dev URI used for localdev seeding"

require-env:
	@test -f .env || { \
	    echo "error: .env not found. Create it with:"; \
	    echo "    cp env.example .env"; \
	    echo "    printf 'PUID=%s\\nPGID=%s\\n' \"\$$(id -u)\" \"\$$(id -g)\" >> .env"; \
	    exit 1; \
	}

# Live Quip Testnet. Bypasses docker-compose.override.yml so the validator boots
# against chain-specs/quip-testnet.json instead of substrate's `dev` preset.
testnet: require-env
	$(COMPOSE_TESTNET) --profile $(PROFILE) pull
	$(COMPOSE_TESTNET) --profile $(PROFILE) up -d
	@echo ""
	@echo "testnet stack up. tail logs: make logs"

# Self-contained dev chain. Relies on docker-compose.override.yml flipping the
# validator to --chain=dev and pulling quip-faucet into the validator profile.
# Order matters: validator+faucet must produce blocks before sudo seeding;
# seeding must complete before the miner bootstraps (so DefaultTopology + the
# difficulty are live when the miner queries them).
localdev: require-env down clean-chain
	$(COMPOSE) --profile $(PROFILE) pull
	$(COMPOSE) --profile $(PROFILE) up -d quip-validator quip-faucet
	@echo "waiting for validator to produce blocks..."
	@sleep 12
	$(COMPOSE) --profile $(PROFILE) run --rm \
	    -v "$(CURDIR)/scripts/seed-advantage2-topology.py:/seed.py:ro" \
	    --entrypoint python3 cpu /seed.py --sudo-key $(SUDO_KEY)
	$(COMPOSE) --profile $(PROFILE) run --rm \
	    --entrypoint quip-miner cpu \
	    bootstrap --validator ws://quip-validator:9944 \
	    --signer-key /data/keystore.json \
	    --faucet-url http://quip-faucet:8087
	$(COMPOSE) --profile $(PROFILE) up -d
	@echo ""
	@echo "localdev stack up. tail logs: make logs"

pull: require-env
	$(COMPOSE) --profile $(PROFILE) pull

down:
	-$(COMPOSE) --profile $(PROFILE) --profile faucet down

logs:
	$(COMPOSE) logs -f --tail=50 quip-validator cpu cuda 2>/dev/null

# `trash` keeps wiped chains recoverable via macOS Trash per global preference;
# the rm fallback covers Linux/CI hosts without `trash` installed.
clean-chain:
	@if command -v trash >/dev/null 2>&1; then \
	    trash data/validator-data/chains 2>/dev/null || true; \
	else \
	    rm -rf data/validator-data/chains; \
	fi
