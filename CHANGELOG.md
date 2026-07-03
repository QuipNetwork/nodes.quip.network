# Changelog

## v0.2 (unreleased)

### Miner is config-driven — `QUIP_*` miner env vars removed

The quip-protocol v0.2.1-rc miner images (which the rolling `:v0.2` registry tag serves) dropped every configuration env var: `data/config.toml` is the single source of truth, and the entrypoint's env contract is `PUID`/`PGID` only. This repo now matches that contract:

- `QUIP_VALIDATORS`, `QUIP_FAUCET_URL`, and `QUIP_REST_PORT` are gone from `docker-compose.yml` — the rc-line images silently ignored them. Set `[miner].validators` / `.faucet_url` / `.rest_port` in `data/config.toml` instead. The miner's built-in validator fallback is `["ws://quip-validator:9944", "ws://127.0.0.1:9944"]`, so the colocated-validator default needs no config at all.
- `caddy/Caddyfile` proxies `/api/v1/*` to `quip-miner:8086` (the image's `rest_port` default) instead of the old forced `:80`.
- First-run configs are seeded from repo-owned templates (`config/quip-miner.{cpu,cuda}.toml`, bind-mounted over `/app/quip-miner.docker.toml`) — identical to upstream's except `faucet_url` is set to the canonical testnet faucet so first-boot auto-funding keeps working.
- `make localdev` copies `config/localdev.<profile>.toml` to `data/config.toml` before bringing the stack up (the localdev stack is self-contained by design); the old `QUIP_FAUCET_URL` env override in `docker-compose.localdev.yml` is gone.
- `make updateconfig` now also handles already-v0.2 configs: it backfills `faucet_url`, `rest_port` (→ 8086), `rest_host`, and validators (harvesting uncommented `QUIP_VALIDATORS` / `QUIP_FAUCET_URL` values from `.env` first), removes an explicitly-empty `validators = []` list so the built-in fallback applies, and strips the dead `QUIP_*` miner lines from `.env`. Backups: `data/config.toml.pre-backfill.bak` and `.env.pre-config-driven_backup`.
- `DWAVE_API_KEY` remains an env var — it's read by the QPU layer (D-Wave Ocean SDK), which also supports `~/.config/dwave/dwave.conf` as a file-based alternative. `CUDA_MPS_*` remain (NVIDIA runtime, no file equivalent).

**Operator impact**: existing v0.2 deployments must run `make updateconfig` (or hand-edit `data/config.toml`) — their configs predate the config-driven images and rely on env overrides that no longer exist. Without the backfill, REST stays disabled (`rest_port = -1`) and auto-funding is off.

### Explicit per-service env contract (no more blanket `env_file`)

`docker-compose.yml` no longer attaches `env_file: .env` to every service. `.env` is now compose's interpolation source only: a variable reaches a container solely when an `environment:` entry wires it through. Previously every `.env` entry — including `POSTGRES_PASSWORD`, `DWAVE_API_KEY`, and `ZEROSSL_API_KEY` — was injected into every container, whether it used them or not.

**Operator impact**: if your `.env` carries a custom variable that a container consumed via the old blanket injection, wire it through a `docker-compose.override.yml` `environment:` entry. The documented variables in `env.example` are unaffected — they were already interpolated or explicitly wired.

Related cleanups in the same pass:

- `PUID`/`PGID` are defined once via a shared `x-runtime-user` YAML anchor instead of being repeated per service.
- `DWAVE_API_KEY` is passed explicitly to the cpu/cuda miners (the only consumers, and only in qpu mode). Slated for removal once the miner reads the token from a file.
- Dropped `QUIP_MODE=gpu` from the cuda service — upstream is config-driven and the cuda image bakes in `QUIP_DEFAULT_MODE=gpu`; the var was never read.
- Dropped `DB_ADAPTER=postgres` from the dashboard — the image selects its adapter from `DATABASE_URL` presence; no such env var exists in the dashboard source.
- Dropped `QUIP_REST_HOST` and `QUIP_SIGNER_KEY` from the miner services — both restated the entrypoint's own defaults (`0.0.0.0`, `/data/keystore.json`).
- Dropped `SUBSTRATE_BOOTNODES` from `env.example` — it was never wired into the validator (compose can't split one env var into multiple `--bootnodes` argv tokens). Private-network operators add `--bootnodes=` flags via `docker-compose.override.yml`.
- Removed the stale `docker-compose.override.dev.yml.bak`.

### Testnet auto-fund on first boot

The seeded `data/config.toml` sets `faucet_url = "https://faucet.testnet.quip.network"` (via the repo's `config/quip-miner.{cpu,cuda}.toml` templates). On a fresh `make testnet` (or `docker compose --profile cpu up -d`) the miner generates the keystore, calls the testnet faucet to register the new account on-chain and fund it, and starts mining — no manual `quip-miner bootstrap` step required. Comment out `faucet_url` in `data/config.toml` to opt out if you pre-fund the account yourself. `make localdev` copies a config pointing at the colocated dev faucet, so localdev continues to use `//Alice` via the bundled `quip-faucet` sidecar.

### `make updateconfig` also migrates `.env`

`scripts/upgrade-config.py` now rewrites the operator's `.env` (sibling of the `data/` it's converting) in addition to the TOML config. It:

- Backs up the existing `.env` to `.env.v0.1_backup` (idempotency guard: refuses to clobber an existing backup).
- Drops `QUIP_NODE_URL` and `QUIP_NODE_TOKEN` entries — commented and uncommented forms both — since those v0.1 dashboard env vars were superseded by `QUIP_VALIDATOR_RPC_URLS` in v0.2. Leaving the stale lines in caused the v0.1 dashboard image's auto-derived public URL fallback to win, sending the indexer's miner-REST poll on a pointless `https://<host>` round-trip through Caddy back to the same container.
- Appends a commented `QUIP_VALIDATOR_RPC_URLS=ws://quip-validator:9944` placeholder so the docker-compose default (the colocated validator alias) is documented in the operator's own file.

Opt out with `python3 scripts/upgrade-config.py data --no-env-file` (or `--env-file PATH` to point at a `.env` outside the default sibling location). Operators on a host with a fresh v0.2 `.env` (no stale keys, has `QUIP_VALIDATOR_RPC_URLS`) see no changes — the migration is conditional on detecting v0.1 markers.

### Auto-bootstrap miner on first start

The `cpu` / `cuda` miner self-bootstraps on startup: its entrypoint funds the new account via the configured faucet and registers it in `QuantumPow.Miners`, retrying until the validator has synced, before it starts producing proofs. This eliminates the `RuntimeError: signer account ... is not in QuantumPow.Miners — run 'quip-miner bootstrap' first` crash loop operators previously hit on fresh keystores — with no separate one-shot bootstrap container.

Idempotent: re-runs on subsequent `up -d` invocations are no-ops once the account is registered. `make localdev` keeps the topology-seeding step, since seeding still has to happen before the miner's self-bootstrap can succeed.

### `docker-compose.override.yml` → `docker-compose.localdev.yml` (opt-in)

The local-dev chain override was renamed off the magic `docker-compose.override.yml` filename so that plain `docker compose --profile cpu up -d` defaults to the live Quip Testnet instead of silently flipping the validator to `--chain=dev` via auto-loaded override.

**Previously**: any `docker compose ...` invocation auto-loaded the override and put the validator on `--chain=dev` (Alice as sole authority, no peers, no registered topology). Operators following README invocations like `docker compose --profile cpu up -d` ended up on dev chain when they meant testnet — visible only as "chain has no registered topology" errors from the miner.

**Now**:
- `docker compose --profile cpu up -d` → testnet (correct out of the box)
- `make localdev` → dev chain (wraps `docker compose -f docker-compose.yml -f docker-compose.localdev.yml`)
- `docker compose -f docker-compose.yml -f docker-compose.localdev.yml --profile cpu up -d` → dev chain (explicit form)

Existing operators with `docker-compose.override.yml` on disk should `git pull` and either delete the leftover file (it's now removed from the repo, but `git pull` won't delete untracked working-copy artifacts) or accept that it'll keep overriding their commands until they remove it manually. `make testnet` continues to bypass any override because it explicitly passes `-f docker-compose.yml`.

### Compose profile collapse

The `validator-cpu` and `validator-cuda` profiles are gone. The `cpu` and `cuda` profiles now bundle the substrate validator + dashboard + Caddy by default, so every operator runs a local validator without needing to opt in. Effects:

- `docker compose --profile cpu up -d` (or `cuda`) now brings up: miner, validator, dashboard, postgres, Caddy.
- `--profile faucet` still layers additively on top.
- `Makefile`'s `PROFILE` default is now `cpu` (was `validator-cpu`).
- TLS is best-effort: if Caddy can provision a cert (HTTP-01 on `:80` or DNS-01), the public RPC is served at `wss://<host>/rpc`. Without it the validator still runs and is reachable on the compose network — only the public WSS endpoint depends on the cert.
- Operators who want a miner-only host pointing at a remote validator still can — set `[miner].validators` in `data/config.toml` to the remote WS URL — but it's no longer the default topology.

### Upgrading from v0.1 — config migration required

The miner config schema changed substantially. Run `make updateconfig` (or `make updateconfig-docker` if the host has Python < 3.11) against your `data/` directory to convert in place. The original files are moved to `data/.v0.1_backup/`; nothing is deleted.

```bash
make updateconfig DATA=path/to/data    # defaults to ./data
```

The converter is idempotent — re-running on an already-converted dir exits cleanly.

The full operator runbook (stop v0.1 containers, pull v0.2, convert config, choose ACME challenge type, bring up the new stack) lives in [README.md → Upgrading from v0.1](README.md#upgrading-from-v01).

#### Schema diff

- **Renamed**: `[global]` → `[miner]`. The catch-all v0.1 section is now scoped to this miner's substrate connection (validator list, keystore, identification).
- **Renamed (binary)**: `quip-node` → `quip-miner`. Example TOML files follow: `quip-node.example.toml` → `quip-miner.example.toml`, `docker/quip-node.{cpu,cuda}.toml` → `docker/quip-miner.{cpu,cuda}.toml`.
- **Added (required)**: `[miner].validators` (ordered failover list of substrate WS URLs), `[miner].signer_key` (path to the sr25519 + ML-DSA-44 hybrid keystore — the entrypoint auto-generates one on first start).
- **Added (optional)**: `[miner].faucet_url`, `[miner].public_host`, `[miner].public_port`.
- **Promoted into `[miner]`**: `log_level`, `node_log` (now rotating 10 MB × 5).
- **Removed (no consumer in v0.2)**: `secret`, `genesis_config`, `auto_mine`, `peer`, `timeout`, `heartbeat_interval`, `heartbeat_timeout`, `fanout`, `verify_tls`, `ca_bundle`, `tls_cert_file`, `tls_key_file`, `rest_tls_cert_file`, `rest_tls_key_file`, `tofu`, `trust_db`, `rest_insecure_port`, `webroot`, `http_log`, `telemetry_enabled`, `telemetry_dir`, and the entire `[telemetry_api]` table. The substrate validator owns p2p, Caddy handles TLS, the REST `/api/v1/*` surface replaces file-based telemetry, and access control is a deployment concern (reverse-proxy auth, network policy) rather than an in-process bearer token.
- **Preserved verbatim**: `[cpu]`, `[gpu]`, `[cuda.N]` / `[nvidia.N]`, `[metal]`, `[modal]`, `[qpu]`, `[dwave]`, `[ibm]`, `[braket]`, `[pasqal]`, `[ionq]`, `[origin]`. Semantics + inheritance rules unchanged.
- **Aliased in the loader (but the converter does NOT use these)**: `[miner].listen` → `[miner].rest_host`, `[miner].port` → `[miner].rest_port`. The aliases exist so a hand-edited file still parses, but the semantics flipped (QUIC peer → telemetry REST). The converter drops `listen` and `port` and emits a warning so an operator with `port = 20049` doesn't accidentally publish the REST API on what used to be the peer port.

#### What the converter does on a v0.1 dir

1. Parses `data/config.toml` with stdlib `tomllib`.
2. Moves every entry in `data/` (except an existing `.v0.1_backup/`) into `data/.v0.1_backup/`.
3. Writes a fresh `data/config.toml` in v0.2 shape, with values harvested from the backed-up file:
   - `node_name`, `public_host`, `public_port`, `rest_host`, `rest_port`, `log_level`, `node_log` carry over.
   - `validators` defaults to `["ws://quip-validator:9944"]` (colocated validator — the common case for `nodes.quip.network`). Edit `[miner].validators` afterwards for miner-only or remote deploys.
   - `signer_key` defaults to `"/data/keystore.json"`.
   - All preserved backend tables (`[cpu]`, `[gpu]`, `[cuda.N]`, `[qpu]`, `[dwave]`, ...) are re-serialized verbatim from the parsed dict.
4. Prints operator-actionable warnings to stderr: dropped `port`/`listen` (semantics flipped), dropped `peer[]` (no P2P mesh anymore), `[telemetry_api]` removed, `[dwave].token` preserved but DWAVE_API_KEY in environment is now the convention.

Comments from the v0.1 file are not preserved — stdlib `tomllib` discards them. The canonical v0.2 template at `data/config.toml` ships with inline documentation; reference it after conversion.

#### `.env` cleanup (manual)

The `make updateconfig` script only touches `data/config.toml`; `.env` is operator-owned and not rewritten. Diff your `.env` against the v0.2 `env.example` and delete the following stale entries:

- `QUIP_NODE_URL` — superseded by `QUIP_VALIDATOR_RPC_URLS`, a comma-separated list of substrate WS URLs that drives both chain indexing and the miner REST surface (Caddy fronts both on the same host). For miner-only nodes, point it at a public full node, e.g. `wss://cpu-1.nodes.quip.network/rpc`.
- `QUIP_NODE_TOKEN` — removed; access control moved out of the dashboard image into the deployment layer (reverse-proxy auth, network policy).

Leaving the stale lines in `.env` is harmless (compose ignores unknown vars), but they're misleading for anyone reading the file later.
