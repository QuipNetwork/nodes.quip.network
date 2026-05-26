# Changelog

## v0.2 (unreleased)

### Testnet auto-fund on first boot

`QUIP_FAUCET_URL` now defaults to `https://faucet.testnet.quip.network` in `docker-compose.yml`. On a fresh `make testnet` (or `docker compose --profile cpu up -d`) the miner entrypoint generates the keystore, calls the testnet faucet to register the new account on-chain and fund it, and starts mining — no manual `quip-miner bootstrap` step required. Set `QUIP_FAUCET_URL=` (empty) in `.env` to opt out if you pre-fund the account yourself. `docker-compose.override.yml` (used by `make localdev`) flips this to the colocated dev faucet, so localdev continues to use `//Alice` via the bundled `quip-faucet` sidecar.

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
- Operators who want a miner-only host pointing at a remote validator still can — set `QUIP_VALIDATORS` in `.env` to the remote WS URL — but it's no longer the default topology.

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
   - `validators` defaults to `["ws://quip-validator:9944"]` (colocated validator — the common case for `nodes.quip.network`). Override via `.env`'s `QUIP_VALIDATORS` for miner-only or remote deploys.
   - `signer_key` defaults to `"/data/keystore.json"`.
   - All preserved backend tables (`[cpu]`, `[gpu]`, `[cuda.N]`, `[qpu]`, `[dwave]`, ...) are re-serialized verbatim from the parsed dict.
4. Prints operator-actionable warnings to stderr: dropped `port`/`listen` (semantics flipped), dropped `peer[]` (no P2P mesh anymore), `[telemetry_api]` removed, `[dwave].token` preserved but DWAVE_API_KEY in environment is now the convention.

Comments from the v0.1 file are not preserved — stdlib `tomllib` discards them. The canonical v0.2 template at `data/config.toml` ships with inline documentation; reference it after conversion.

#### `.env` cleanup (manual)

The `make updateconfig` script only touches `data/config.toml`; `.env` is operator-owned and not rewritten. Diff your `.env` against the v0.2 `env.example` and delete the following stale entries:

- `QUIP_NODE_URL` — superseded by `QUIP_VALIDATOR_RPC_URLS`, a comma-separated list of substrate WS URLs that drives both chain indexing and the miner REST surface (Caddy fronts both on the same host). For miner-only nodes, point it at a public full node, e.g. `wss://cpu-1.nodes.quip.network/rpc`.
- `QUIP_NODE_TOKEN` — removed; access control moved out of the dashboard image into the deployment layer (reverse-proxy auth, network policy).

Leaving the stale lines in `.env` is harmless (compose ignores unknown vars), but they're misleading for anyone reading the file later.
