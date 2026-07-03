# AGENTS.md — `nodes.quip.network`

Infrastructure-as-code for Quip Network operator nodes. Docker Compose deployment that bundles a substrate validator, a quantum-PoW miner, the dashboard SPA, Caddy reverse proxy with auto-TLS, and a Postgres backend into a single-host stack.

If you're an AI agent working on this repo, read this file first. It's the cross-tool standard ([read by Claude Code, Codex, Cursor, etc.](https://agents.md)). The [README.md](README.md) is the operator-facing equivalent; this file is the agent-facing version with extra context.

## Purpose

- Make it trivial for an operator to join the live Quip Testnet (`make testnet`) or spin up a self-contained dev chain (`make localdev`).
- Compose all the moving pieces (validator, miner, dashboard, postgres, Caddy) into a single `--profile cpu up -d` flow with no manual chain-state seeding.
- Provide an in-place v0.1 → v0.2 config migration (`make updateconfig`) so existing operators don't have to hand-edit configs across two breaking schema changes (TOML + `.env`).

## Invariants (do not violate)

- **No operator key generation.** Operator mnemonics + libp2p node keys are derived once via `scripts/derive-operator-keys.sh` in `quip-protocol-rs`. This repo only *uses* keys mounted at `data/keystore.json` and `data/node-key`.
- **No secrets in git.** `data/` content (keystores, mnemonics, node keys, signing.json) is gitignored. The `tests/fixtures/v0.1/qpu/data/config.toml` D-Wave token is `DEV-FIXTURE-FAKE-TOKEN-DO-NOT-USE` — never use a real token in fixtures.
- **Do not tag v0.2.0** in `quip-protocol-rs` from this session. That's a coordinated release step.
- **No direct pushes to `main`.** Work on `v0.2` (or a feature branch). MR !4 is the v0.2 → main integration MR.
- **No `Co-Authored-By: Claude` trailers.** This repo follows the global CLAUDE.md commit standard — no LLM attribution in commits.
- **Use `trash` instead of `rm -rf`** (hook-enforced for `rm -rf`). Falls back to `rm -rf` on hosts without `trash` installed (CI/Linux servers); the macOS workflow expects `trash` for recoverable deletes.
- **Prefer dedicated tools over Bash.** `Read`/`Edit`/`Write` over `cat`/`sed`/`echo`.

## Repo layout

| Path | Purpose |
|---|---|
| `docker-compose.yml` | Canonical stack — testnet by default. Validator bundled into `cpu`/`cuda` profiles. |
| `docker-compose.localdev.yml` | **Opt-in only.** Layered on top of base via `make localdev` or explicit `-f` flags. Flips validator to `--chain=dev`, adds `quip-faucet` sidecar, and **namespaces the whole stack** — every service gets a `-localdev` `container_name` and the volumes get a `quip-localdev-` prefix, run under the `-p quip-localdev` project, so it coexists with a live testnet stack instead of colliding over the fixed global names (`quip-postgres`, `quip-validator`, …) in the base file. Was previously `docker-compose.override.yml` — see migration notes below. |
| `caddy/Caddyfile` | Reverse proxy + auto-TLS. Routes `/rpc` → `quip-validator:9944`, `/api/faucet/*` → `quip-faucet:8087`, `/api/v1/*` → `quip-miner:80`, `/` → `quip-dashboard:3001`. |
| `chain-specs/quip-testnet.json` | Mirrored from `quip-protocol-rs` via `build-spec --chain quip-testnet --raw`. Re-export when upstream genesis changes. |
| `chain-specs/quip-testnet.json.sha256` | SHA-256 checksum sidecar — always update alongside the spec. |
| `data/config.toml` | Canonical v0.2 `[miner]` template (gitignored copy lives at operator's `data/config.toml`). |
| `data/config.cpu.toml`, `data/config.cuda.toml` | Mode-specific templates operators `cp` to `data/config.toml` on first run. |
| `data/chain-spec.json` | Local dev chain spec (`quip-local` preset). |
| `scripts/upgrade-config.py` | v0.1 → v0.2 config converter. Stdlib-only Python 3.11+. Migrates both `data/config.toml` and the sibling `.env`. |
| `scripts/seed-advantage2-topology.py` | One-shot sudo extrinsic submitter — registers `advantage2_system1` as `DefaultTopology` + sets `Difficulty`. Takes `--sudo-key` (dev URI or hex master seed) or `--mnemonic-file`. |
| `scripts/sysctl-tune.sh` | Host kernel tuning (BBR + fq + no slow-start-after-idle). |
| `tests/fixtures/v0.1/{cpu,cuda,qpu,already-v0.2}/data/config.toml` | Trimmed real operator configs used by the converter test suite. |
| `tests/test_upgrade_config.py` | 28 pytest cases against `scripts/upgrade-config.py`. |
| `cron.sh` | Auto-update sidecar (hourly cron) — detects running profiles from container names, pulls + recreates only on digest change. |
| `Makefile` | Operator entry points: `make testnet`, `make localdev`, `make updateconfig`, etc. |
| `env.example` | Template for `.env`. Read alongside `docker-compose.yml` to see all defaults. |
| `CHANGELOG.md` | Operator-facing release notes — same v0.2 changes documented here, but framed as "what changes" rather than "how the agent should reason." |
| `docs/testnet-deployment.md` | Bootnode operator runbook (libp2p key, BABE/GRANDPA session keys, ports). |
| `CADDY.md` | TLS / Caddy operator notes. |

## Working conventions

### Modifying compose

After editing `docker-compose.yml` or `docker-compose.localdev.yml`, always validate both modes parse:

```bash
docker compose config --profiles
docker compose -f docker-compose.yml -f docker-compose.localdev.yml --profile cpu config --services
```

Profile membership changes (services moving between `cpu`/`cuda`/`faucet`) need to be cross-checked against `cron.sh` `detect_profile()` — the auto-update logic infers profiles from running container names (`^quip-cpu$`/`^quip-cuda$`/`^quip-faucet$`). That matches the **testnet** names only: localdev's `-localdev`-suffixed containers are intentionally excluded, so the hourly cron never recreates a dev stack as testnet.

When adding a service to the base file, also add a `-localdev` `container_name` override (and any named volume) to `docker-compose.localdev.yml` — otherwise the new service reuses the fixed global name and breaks coexistence with a running testnet stack. Validate every service is namespaced:

```bash
docker compose -p quip-localdev -f docker-compose.yml -f docker-compose.localdev.yml --profile cpu config \
  | awk '/^services:/{s=1} /^volumes:/{s=0} s && /container_name:/{print}'
```

Every line should end in `-localdev`.

### Adding a Make target

Prefer thin wrappers over `docker compose` invocations. `PROFILE` is parameterized (`?= cpu`). Use `COMPOSE` for testnet (plain `docker compose`) or `COMPOSE_LOCALDEV` for localdev (`docker compose -p quip-localdev -f docker-compose.yml -f docker-compose.localdev.yml` — the distinct `-p` project name is what isolates the dev stack). Don't auto-load any `docker-compose.override.yml` — the rename was deliberate, see migration notes.

Because testnet (default project) and localdev (`-p quip-localdev`) are now separate projects, any teardown target must hit **both** — `make down` runs `docker compose down` once per project. `PROFILE=cuda` testnet boots also depend on `require-mps` (see the GPU/MPS note below); it's a no-op for `PROFILE=cpu`.

### GPU mining / NVIDIA MPS

The `cuda` service is wired for [NVIDIA MPS](https://docs.nvidia.com/deploy/mps/) (hardware SM sharing): `ipc: host`, `pid: host`, a `/tmp/nvidia-mps` bind-mount, and `CUDA_MPS_PIPE_DIRECTORY` + `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` (driven by `QUIP_GPU_UTILIZATION`, default 100). **MPS is a host facility** — the control daemon runs on the host, not in any container, so the image alone cannot enable it; the `MPS not active in container` miner log is correct graceful degradation, not an image bug. MPS is unsupported under WSL2 / Docker Desktop.

`make testnet PROFILE=cuda` runs the `require-mps` target first, which idempotently starts `nvidia-cuda-mps-control -d` on the host (pipe dir `/tmp/nvidia-mps`, matching the compose bind-mount). Best-effort: a missing binary or a failed start (needs root, or unsupported host) only warns — the miner still boots, degraded. `require-mps` is a no-op for `PROFILE=cpu`. The compose settings are inert (not harmful) on hosts without a running daemon, so `cron.sh`'s plain `docker compose up -d` recreate path is safe — it rejoins the already-running host daemon without restarting it.

### Editing the converter

`scripts/upgrade-config.py` is stdlib-only on purpose — runs without `pip install` on any Python 3.11+ host, plus inside `python:3.12-alpine` (the docker fallback). When you add behavior, also add a pytest case in `tests/test_upgrade_config.py` using the existing `_copy_fixture` pattern, and run the full suite (`python3 -m pytest tests/test_upgrade_config.py -q`).

### Chain spec drift

`chain-specs/quip-testnet.json` mirrors the upstream `quip-protocol-rs` v0.2 image's baked-in spec. If `shasum -a 256 -c chain-specs/quip-testnet.json.sha256` fails after an upstream image bump, re-export:

```bash
docker run --rm registry.gitlab.com/quip.network/quip-protocol-rs/quip-network-node:v0.2 \
  build-spec --chain quip-testnet --raw > chain-specs/quip-testnet.json
(cd chain-specs && shasum -a 256 quip-testnet.json > quip-testnet.json.sha256)
```

A stale chain spec presents as: validator's libp2p connects fine, but `system_health.peers=0` because the substream-level handshake rejects on genesis-hash mismatch. Real symptom is silent — debug logs (`-l sub-libp2p=debug,litep2p=debug`) show `failed to negotiate substream ... block-announces`.

### Verifying ports

`check.quip.network/checkport?port=N` self-checks the caller's public IP. Use it (not a third-party port checker) so the operator host's source IP is what gets tested. Documented in README architecture section.

### Commits

- Imperative mood, ≤72 char subject.
- Body explains *why*, references the symptom or operator feedback that motivated the change.
- One logical change per commit. Multi-file changes are fine if they form one coherent shape (e.g., docker-compose.yml + Makefile + README all reflecting a profile rename).
- `git push origin v0.2` — never to `main`.

## Operator workflows

```
make testnet     →  (PROFILE=cuda: start host MPS daemon) docker compose --profile cpu up -d   (live testnet)
make localdev    →  docker compose -p quip-localdev -f docker-compose.yml -f docker-compose.localdev.yml --profile cpu up -d   (dev chain, own namespace)
make updateconfig → python3 scripts/upgrade-config.py data                          (v0.1 → v0.2)
make down        →  tear down BOTH projects (testnet default + quip-localdev)
make clean       →  down + wipe chain + drop pgdata + quip-localdev-pgdata volumes + wipe dashboard-data   (destructive)
```

The flow on a clean `make testnet` boot:

1. `quip-validator` starts → joins testnet via the three bootnodes embedded in `chain-specs/quip-testnet.json`.
2. `cpu` (or `cuda`) miner starts → self-bootstraps on startup: funds its account via `https://faucet.testnet.quip.network` and registers it in `QuantumPow.Miners` (retrying until the validator has synced), then mines against `DefaultTopology = advantage2_system1`.
3. `dashboard` + `postgres` + `caddy` come up alongside.

---

# v0.1 → v0.2 Migration Reference

This is the comprehensive record of what changed between the v0.1 P2P-mesh node and the v0.2 substrate-validator + RPC-client miner architecture. Operator-facing version of much of this is in [CHANGELOG.md](CHANGELOG.md); this version emphasizes the *why* so future agents understand the constraints.

## The big shift

v0.1 was a P2P mesh of `quip-node` processes. Each node owned its own libp2p stack, served its own TLS-terminated REST + WebSocket surface, and propagated work via QUIC peer connections. Mining proofs flowed peer-to-peer in a gossip overlay.

v0.2 replaces the P2P mesh with a **substrate parachain** (`quip-protocol-rs`). Each operator now runs:
- a **substrate validator** (`quip-network-node`) that participates in consensus via libp2p `:30333` and serves substrate RPC at `:9944` (Caddy-fronted at `/rpc`)
- a **miner** (`quip-miner`) that's a *client* of the validator: pure outbound WebSocket RPC, no inbound listeners
- the **dashboard** + **Caddy** + **Postgres** (same as v0.1 but reconfigured)

Every key that was "miner-as-peer" (P2P, TLS at the miner, gossip, TOFU pinning) lost its consumer. Every key that's "miner-as-RPC-client" (validator URLs, hybrid keystore, faucet auto-fund) is new.

## Binary + image renames

| v0.1 | v0.2 | Notes |
|---|---|---|
| `quip-node` | `quip-miner` | Binary inside the miner image. |
| `quip-network-node-cpu` (image) | `quip-miner-cpu` | Same trick for CUDA. |
| `docker/quip-node.cpu.toml` | `docker/quip-miner.cpu.toml` | Image-baked config template. |
| `systemd-linux/quip-node.systemd.toml` | `systemd-linux/quip-miner.systemd.toml` | Systemd unit. |
| _new_ | `quip-network-node` | The substrate validator binary (from `quip-protocol-rs`). |

## Config schema (`data/config.toml`)

**Top-level rename**: `[global]` → `[miner]`. The catch-all v0.1 section is now scoped to the miner's substrate connection (validator list, keystore, identification).

| v0.1 key | v0.2 destination | Notes |
|---|---|---|
| `[global]` (table) | `[miner]` | Rename. |
| `[global].node_name` | `[miner].node_name` | Most important field — carried over by the converter. |
| `[global].public_host`, `.public_port` | `[miner].public_host`, `.public_port` | Promoted to first-class (was commented in v0.1). |
| `[global].rest_host` | `[miner].rest_host` | Direct copy. |
| `[global].rest_port` | `[miner].rest_port` (**forced to 80**) | Caddy now proxies `/api/v1/*` to `quip-miner:80`. v0.1 values like 443 (miner-terminated TLS) break the Caddy upstream. Converter forces 80 + warns. |
| `[global].log_level`, `.node_log` | `[miner].log_level`, `.node_log` | Promoted; `node_log` is now rotating 10MB × 5 (was unbounded in v0.1). |
| `[global].secret` | dropped | Deterministic-key seed replaced by hybrid sr25519 + ML-DSA-44 keystore at `signer_key`. |
| `[global].genesis_config` | dropped | Genesis owned by validator (chain spec baked into binary). |
| `[global].auto_mine` | dropped | Miner mines unconditionally once connected. |
| `[global].peer = [...]` | dropped | No P2P mesh. Use `[miner].validators` for substrate RPC failover. |
| `[global].timeout`, `.heartbeat_*`, `.fanout` | dropped | QUIC peer timeouts, no consumer. |
| `[global].verify_tls`, `.ca_bundle`, `.tls_cert_file`, `.tls_key_file`, `.rest_tls_*`, `.tofu`, `.trust_db` | dropped | TLS termination moved to Caddy. |
| `[global].rest_insecure_port`, `.webroot` | dropped | Collapsed into single `rest_port`; Caddy handles ACME. |
| `[global].http_log` | dropped | aiohttp logger no longer split out — shares `node_log`. |
| `[global].telemetry_enabled`, `.telemetry_dir` | dropped | File-based per-block telemetry replaced by `/api/v1/*` REST surface. |
| `[telemetry_api]` table | dropped | In-process bearer-token auth gone; access control is deployment-layer (reverse-proxy auth, network policy). |
| `[miner].validators` | **new, required** | Ordered failover list of substrate WS URLs. Default in v0.2: `["ws://quip-validator:9944"]` (the colocated bundled validator). |
| `[miner].signer_key` | **new, required** | Path to the hybrid sr25519 + ML-DSA-44 keystore. Default `/data/keystore.json`; entrypoint auto-generates on first start. |
| `[miner].faucet_url` | **new, optional** | Dev auto-topup. Production: `https://faucet.testnet.quip.network` (docker-compose default). Set empty to opt out. |
| `[cpu]`, `[gpu]`, `[cuda.N]`, `[nvidia.N]`, `[metal]`, `[modal]`, `[qpu]`, `[dwave]`, `[ibm]`, `[braket]`, `[pasqal]`, `[ionq]`, `[origin]` | **preserved verbatim** | Backend tuning unchanged. Mode selection is now driven by which backend sections are present (`quip-miner resolve-modes` reads the config), not by env vars. |

**Loader aliases (do NOT rely on)**: `[miner].listen` → `rest_host`, `[miner].port` → `rest_port`. The v0.2 loader silently rewrites these for copy-paste safety, but semantics flipped (v0.1 QUIC peer port → v0.2 telemetry REST). The converter drops `listen`/`port` with a loud warning instead of using the alias, to prevent an operator with `port = 20049` from accidentally exposing the REST API on what used to be the peer port.

## `.env` schema

| v0.1 var | v0.2 destination | Notes |
|---|---|---|
| `QUIP_NODE_URL` | `QUIP_VALIDATOR_RPC_URLS` | Plural; comma-separated; drives both chain indexing and the miner REST surface that Caddy fronts on the same host. Forward-looking name; upstream dashboard image migration pending — currently the image still reads `QUIP_NODE_URL`. |
| `QUIP_NODE_TOKEN` | dropped | Bearer-token access control is now deployment-layer. |
| `QUIP_HOSTNAME` | unchanged (semantics expanded) | Drives Caddy listen + TLS. Comma-separated form (`host, host:20049`) needed for prod TLS. |
| `QUIP_VALIDATORS` | new | Optional CLI override for `[miner].validators`. Defaults to `ws://quip-validator:9944` in docker-compose. |
| `QUIP_REST_PORT` | new | CLI override for `[miner].rest_port`. Compose pins it to 80 so Caddy's `quip-miner:80` upstream works. (`QUIP_SIGNER_KEY`/`QUIP_REST_HOST` exist upstream but compose no longer sets them — the entrypoint defaults already match.) |
| `QUIP_FAUCET_URL` | new | Defaults to `https://faucet.testnet.quip.network` in `docker-compose.yml`; `docker-compose.localdev.yml` overrides to `http://quip-faucet:8087`. |
| `QUIP_VALIDATOR_TAG`, `QUIP_VALIDATOR_RPC_URLS`, `QUIP_FAUCET_TAG`, `QUIP_FAUCET_NODE_URL`, `QUIP_FAUCET_KEY`, `QUIP_FAUCET_RATE_LIMIT_SECONDS`, `QUIP_FAUCET_ALLOW_ANY_CHAIN`, `VALIDATOR_NAME`, `CERT_EMAIL`, `ZEROSSL_API_KEY`, `QUIP_MINER_CPUSET`, `QUIP_CHAIN_SPEC`, `QUIP_DASHBOARD_TAG` | new | See `env.example` for inline docs. |

`.env` is compose's interpolation source only — there is no blanket `env_file:` anywhere in `docker-compose.yml`, so a variable reaches a container only when an `environment:` entry wires it through. `SUBSTRATE_BOOTNODES` was dropped entirely (compose can't split one env var into multiple `--bootnodes` argv tokens; use a `docker-compose.override.yml`).

The converter (`scripts/upgrade-config.py`) migrates `.env` alongside `data/config.toml`: backs up to `.env.v0.1_backup`, strips `QUIP_NODE_URL`/`QUIP_NODE_TOKEN` (commented or uncommented), appends a commented `QUIP_VALIDATOR_RPC_URLS` placeholder.

## Compose topology

| Aspect | v0.1 | v0.2 |
|---|---|---|
| Validator | n/a (no chain) | New service `quip-validator`; bundled into the `cpu` and `cuda` profiles by default. |
| Profiles | `cpu`, `cuda`, `qpu` (separate) | `cpu`, `cuda`, `faucet`. QPU collapsed into `cpu` profile + `[qpu]`/`[dwave]` config sections. `validator-cpu`/`validator-cuda` collapsed (briefly existed, removed when the validator became default-bundled). |
| Override file | `docker-compose.override.yml` (auto-loaded by `docker compose`) | `docker-compose.localdev.yml` (**opt-in**; not auto-loaded). Renamed deliberately so plain `docker compose --profile cpu up -d` boots testnet instead of silently flipping to `--chain=dev`. Operators who carry over a stale `docker-compose.override.yml` working-copy file will keep getting the dev chain until they remove it. |
| Faucet | not present | `quip-faucet` service in the `faucet` profile (testnet) or wired into `cpu`/`cuda` via the localdev override (dev). |
| Bootstrap | manual | The `cpu`/`cuda` miner self-bootstraps on startup: it auto-funds via `QUIP_FAUCET_URL` and registers itself in `QuantumPow.Miners` (retrying until the validator has synced) before it begins mining. No separate bootstrap container. |
| Dashboard indexer | `QUIP_NODE_URL` (miner REST) | `QUIP_VALIDATOR_RPC_URLS` (substrate WS — drives both chain indexing and miner-REST polling on the same Caddy-fronted host). Upstream image migration pending. |

## Network / port layout

| Port | v0.1 | v0.2 |
|---|---|---|
| `30333/tcp+udp` | n/a | libp2p peer transport (substrate). Strongly recommended for inbound — your validator becomes a useful peer instead of a leaf. |
| `9944/tcp` | n/a | Substrate RPC (internal; Caddy proxies `/rpc` to it). Not host-published. |
| `9615/tcp` | n/a | Substrate Prometheus metrics (internal). |
| `20049/tcp` | host-published; quip-node REST | Caddy public API port. Same routes as `:443`. Required for operator deployments. |
| `80/tcp`, `443/tcp` | n/a | Caddy ACME HTTP-01 + HTTPS. Only needed if using HTTP-01 cert challenge (DNS-01 alternative is documented in README). |
| Miner REST | host-bound, miner-terminated TLS at `rest_port` (often 443) | internal `quip-miner:80`; Caddy proxies `/api/v1/*` to it. Operators with `rest_port = 443` from v0.1 break the proxy until they (or the converter) sets it to 80. |
| Validator-to-validator gossip | n/a (P2P mesh) | libp2p TCP + QUIC on `:30333`. Bootnodes hardcoded in chain spec. |

Verify any port from the public internet with `curl https://check.quip.network/checkport?port=N` — the service uses the caller's source IP, so run from the host you want to test.

## Chain spec

- v0.1 had no on-chain state. v0.2 introduces `chain-specs/quip-testnet.json` (mirrored from `quip-protocol-rs`).
- The genesis hash MUST match what bootnodes are serving. Mismatch presents as silent peering failure: libp2p layer connects, substream-level handshake rejects. Re-export procedure documented in README.
- `chain-specs/quip-testnet.json.sha256` is the checksum sidecar — always re-export both files together.

## Topology + difficulty (on-chain state)

- `QuantumPow.DefaultTopology` must be set on chain before any miner can submit a proof — otherwise miners exit with `chain has no registered topology; run 'quip-miner bootstrap --seed-chain' first`.
- `scripts/seed-advantage2-topology.py` is the operator tool for this. Takes `--sudo-key` (dev URI or 32-byte hex master seed) or `--mnemonic-file` (path to a BIP39 phrase; derives the hybrid master seed via `substrateinterface.Keypair.create_from_mnemonic` — the BIP39 mini-secret-key is the same input the Rust `sr25519_mldsa44::Pair::from_string(mnemonic)` derivation uses).
- Default difficulty is set in the same script run: `min_solutions = 5`, `max_energy_milli = -2_500_000`, `min_diversity_milli = 200`. The chain's difficulty controller adjusts the live threshold from there based on submission rate.
- Sudo on the testnet is the operator-1 hybrid account (`5GZMo…aYi`) from `quip-protocol-rs/quip-testnet-keys/operator-1/`. Same account is also the faucet funder.

## Faucet

- v0.1: not present.
- v0.2 testnet: `https://faucet.testnet.quip.network` (a separate Docker host running the `quip-faucet` image with operator-1 as the funder). Public; rate-limited per destination.
- v0.2 localdev: `quip-faucet` sidecar in the localdev override, running `//Alice` as the funder. Pre-funded at genesis.
- The miner's entrypoint auto-calls the faucet on first boot if `QUIP_FAUCET_URL` is set — the miner self-bootstraps, no manual step (or separate bootstrap container) required.

## Operator workflow changes

| Workflow | v0.1 | v0.2 |
|---|---|---|
| First boot | edit `config.toml`, `docker compose up -d` | `make updateconfig` (if migrating); `make testnet`; everything else automatic. |
| Funding the miner | manual transfer | Auto — the miner self-bootstraps via `QUIP_FAUCET_URL`. |
| Registering the miner on chain | n/a | Auto — the miner self-registers in `QuantumPow.Miners` on startup. |
| Stopping the stack | `docker compose down` | `make down` (handles both profile sets). |
| Auto-update | hourly cron via `cron.sh` (detects profiles from container names) | Same — `cron.sh` rewrites itself in v0.2 to drop `validator-cpu`/`validator-cuda` branches and detect `cpu`/`cuda`/`faucet` independently. |
| Choosing dev vs testnet | dev was *implicit* via auto-loaded `docker-compose.override.yml` (foot-gun) | `make testnet` (default) vs `make localdev` (explicit `-f docker-compose.localdev.yml`). |

## Known footguns (in this v0.2 stack)

1. **Stale `docker-compose.override.yml` working-copy file.** `git pull` from the v0.2 rename commit (`d5c7ac3`) doesn't delete operator working-copy files. An untracked `docker-compose.override.yml` will still auto-load and override every `docker compose` call to the dev chain. Symptom: validator logs `📋 Chain specification: Development` instead of `Quip Testnet`. Fix: `rm docker-compose.override.yml` on the operator host.
2. **`rest_port` semantic flip.** v0.1 had operators put any port (commonly 443) for miner-terminated TLS. v0.2 needs `rest_port = 80` so Caddy can proxy. Converter forces 80 + warns; operators editing config by hand can still misconfigure.
3. **Chain spec drift.** Genesis hash changes upstream → silent peering failure. Always re-export `chain-specs/quip-testnet.json` after pulling a new `quip-protocol-rs` image.
4. **Topology must be seeded before miners join a fresh testnet.** No bootstrap path exists for miners until sudo seeds `DefaultTopology` via `scripts/seed-advantage2-topology.py`.
5. **Dashboard env-var rename pending upstream.** This repo's `docker-compose.yml` + `env.example` use `QUIP_VALIDATOR_RPC_URLS` (plural). The current v0.2 dashboard image still reads `QUIP_NODE_URL` and `QUIP_VALIDATOR_RPC_URL` (singular). Until the upstream dashboard image migration lands, operators see `substrate=disabled` in dashboard logs. Workaround: hand-add `QUIP_NODE_URL=http://quip-miner:80` and `QUIP_VALIDATOR_RPC_URL=ws://quip-validator:9944` to `.env`. Tracked in the open changes for `dashboard.quip.network`.
6. **Non-root container can bind `:80`.** The miner runs as `uid=1000` but the upstream image grants `CAP_NET_BIND_SERVICE` (or equivalent), so binding `:80` works inside the container. Don't add a `:80` → `:8080` workaround thinking the unprivileged-port limit applies; it doesn't here.
7. **QPU mode selection is now config-driven** (post upstream entrypoint rework). The image's entrypoint calls `quip-miner resolve-modes --config /data/config.toml` and spawns one `quip-miner <mode>` child per resolved backend. Earlier guidance about needing `QUIP_MODE=qpu` env var no longer applies — uncommenting `[qpu]` + `[dwave]` in the config is sufficient (plus `DWAVE_API_KEY` in `.env`).
8. **NVIDIA MPS is host-side.** The `MPS not active in container` miner log means no host MPS daemon, not an image defect. `make testnet PROFILE=cuda` starts it (`require-mps`); raw `docker compose --profile cuda up -d` does not — start `nvidia-cuda-mps-control -d` (likely as root) yourself first, or accept the software-nonce fallback. Unsupported under WSL2 / Docker Desktop. See the GPU/MPS working-conventions note.
9. **Testnet and localdev are separate compose projects.** localdev runs under `-p quip-localdev` with `-localdev` container names; a single `docker compose down` only reaches one project. Use `make down` (hits both). Namespacing fixes *name* collisions, not *host-port* collisions — running both stacks at once still contends for `:80`/`:443`/`:20049`/`:30333`.

## See also

- [README.md](README.md) — operator-facing deployment guide.
- [CHANGELOG.md](CHANGELOG.md) — same v0.1 → v0.2 changes, framed as release notes.
- [CADDY.md](CADDY.md) — TLS / Caddy operator notes.
- [docs/testnet-deployment.md](docs/testnet-deployment.md) — bootnode operator runbook.
- [`quip-protocol-rs/docs/genesis-quip-testnet.md`](https://gitlab.com/quip.network/quip-protocol-rs/-/blob/v0.2/docs/genesis-quip-testnet.md) — upstream genesis + authorities.
- [`quip-protocol-rs/docs/testnet-keys.md`](https://gitlab.com/quip.network/quip-protocol-rs/-/blob/v0.2/docs/testnet-keys.md) — operator key derivation.
