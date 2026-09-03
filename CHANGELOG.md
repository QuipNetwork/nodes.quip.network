# Changelog

## v0.3.0-rc1

Aglais lands on a new minor line. v0.2.x stays on the retired testnet, so an
operator on a stable release keeps the network they are already on and moves
only when they choose an rc.

### Aglais is the default network

Aglais is the Quip test network, in the way Sepolia is the Ethereum test
network. It started on 2026-09-02 from a fresh genesis (runtime 117, transaction
version 7, token `AGLS`) and replaces the previous testnet. The previous
testnet retires after operators move. This stack joins Aglais by default.

- `chain-specs/aglais-network.json` replaces `chain-specs/quip-testnet.json`.
  The `wipe-rc4` validator image exports it. Genesis is `0x59e0…c286`. The
  bootnodes are `bootnode-{1,2,3}.aglais.quip.network`.
- The validator base path moved from `data/validator-data` to
  `data/aglais-chain-db`. Aglais keeps the chain id `quip_testnet`, so a shared
  directory would hand the old database to the new spec, which rejects it on
  genesis mismatch.
- `QUIP_VALIDATOR_TAG` defaults to `wipe-rc4` instead of `latest`. `latest`
  still points at the pre-Aglais image. A later rc release moves the pointer.
- `make updateconfig` rewrites a `faucet_url` that points at the retired
  testnet faucet to `https://faucet.aglais.quip.network`. New configs get the
  Aglais faucet.
- `config/quip-miner.toml` is mounted over the miner image's first-run
  template (`/app/config.toml`) in the `cpu` and `cuda` services. The
  upstream template in `v0.3.1-rc3-aglais-prerelease` still names the retired
  faucet, so a fresh install seeded from the image would fund against the
  wrong chain.
- `config/config.example.toml` is new. It documents every key of the v0.3
  schema: the default for each one and the reason it exists. Nothing mounts
  it. Use it as a reference when editing `data/config.toml`.
- `config/quip-miner.toml` sets `[dashboard].listen` to `0.0.0.0:8086`.
  Upstream's template says `20100`, but `caddy/Caddyfile` proxies `/api/v1/*`
  to `quip-miner:8086`. Because this file seeds `data/config.toml` on first
  run, upstream's port would have left a fresh install with a REST surface
  Caddy cannot reach. Existing operators are unaffected: their
  `data/config.toml` already exists and is never overwritten.

### Difficulty re-baselined for the first proof

The active topology's difficulty was copied from the retired testnet, where it
was the product of a curve that had ramped over thousands of wins. Transplanted
onto a chain with no proof history there was nothing to ramp from, and no proof
had ever landed. The QPU reached a stash of -14,472 against a target of -14,564
and could not cross it.

- `max_energy_milli` moved from `-14563316` to `-14000000`. `min_solutions` and
  `min_diversity_milli` are unchanged at 1 and 0.
- `make updateconfig` repairs a `[dashboard].listen` still on port 20100 and
  reports any other port that is not the one `caddy/Caddyfile` proxies
  `/api/v1/*` to. The dashboard reaches the local miner only through that
  proxy, so a mismatch leaves the UI on "Connecting to miner" while the
  message blames the chain instead. 20100 is repaired because it came from the
  template this repo shipped rather than from the operator. Any other port is
  reported and left alone, since an operator who moved it also edited the
  Caddyfile. The host part is preserved either way.

### Aglais tracks the live Advantage2_system1 working graph

The registered topology drifted from the hardware. D-Wave calibrated coupler
(938, 2812) out, so the chain described a graph with 41,515 edges while the QPU
had 41,514. Every drifted edge is a defect the miner routes around.

- `DefaultTopology` moved from `0xe66d3dfa…3693a02d` to
  `0xcbec1eb4…0c3270e7`, dumped from the live solver. Node set is unchanged at
  4,577.
- Difficulty for the new topology is baselined at the values the chain already
  ran: `min_solutions=1`, `max_energy_milli=-14563316`, `min_diversity_milli=0`.
  It was set before the repoint, so the active default was never without a
  difficulty entry.
- The previous topology stays registered and mineable. Only the default moved.
- `config/advantage2-system1-h0.spec.json` is the dumped live graph and matches
  what the chain now runs.

### D-Wave solver and token now reach the miner

- `docker-compose.yml` passes `DWAVE_API_TOKEN`, `DWAVE_API_SOLVER`, and
  `DWAVE_API_REGION` to the `cpu` and `cuda` services. It previously passed
  only `DWAVE_API_KEY`, a name nothing reads: the miner checks
  `DWAVE_API_TOKEN` (`ocean.py`, `cli.py`) and otherwise lets the Ocean SDK
  resolve its own canonical variables. A QPU node therefore had no way to
  select a solver through this stack, and the SDK fell back to the account
  default, which may not be the Advantage2 system the chain topology targets.
- `DWAVE_API_KEY` still works. Compose maps it into `DWAVE_API_TOKEN` when the
  canonical name is unset, so an existing `.env` needs no edit. An empty value
  resolves the same as an unset one, verified against `dwave.cloud.config`.
- Credentials remain unavailable in `config.toml`. The `[dwave]` table accepts
  budget and anneal keys only, which the miner enforces.

### `make updateconfig` migrates v0.2 to v0.3

- `[miner].rest_host` and `[miner].rest_port` are removed and the REST surface
  moves to `[dashboard].listen`. The v0.3 coordinator names both keys when it
  rejects a config, so leaving them in place is not harmless. The old values
  are not carried over, because the port must match `caddy/Caddyfile`.
- A `[cpu]` table with no `binary` gets `quip-cpu-sa`, the bundled default.
  v0.3 selects the miner variant with this key.
- A config whose only backend is a QPU (`[dwave]`/`[qpu]`) is reported. Such a
  node drops every job while the QPU access-time budget is spent, because the
  coordinator has no other capable backend to re-route to, so it mines nothing
  between refills. Adding `[cpu]` absorbs the rejections.
- A config that names no mining backend is reported, not repaired. v0.3
  refuses to start without one of `[cpu]`, `[cuda.N]`, `[metal]`,
  `[dwave]`/`[qpu]`, and choosing an operator's mining hardware is not the
  converter's decision.
- The `validators` default is now
  `["ws://quip-validator:9944", "ws://127.0.0.1:9944"]`. The converter
  previously wrote only the first entry, which silently removed the loopback
  fallback that an untouched config gets from the coordinator itself.
- The documented export procedure passes `--entrypoint
  /usr/local/bin/quip-network-node`. The image entrypoint prints a line to
  stdout before the JSON, which corrupts a plain redirect.
- `data/chain-spec.json` (the `quip-local` preset) is removed. It carried the
  pre-Aglais runtime, and the current image no longer has that preset. Local
  development uses `make localdev`, which runs the image's `--chain=dev`.
  `QUIP_CHAIN_SPEC` remains for private networks.
- The bootnode runbook inserts session keys with `insert-hybrid-key` and the
  `hybrid-babe-h444` / `hybrid-grandpa-h244` schemes. The stock `key insert`
  schemes produce keys the runtime 117 genesis does not accept.
- The dashboard Postgres volume is `aglais-pgdata`, renamed from
  `quip-pgdata`. The indexer scans from genesis and keys nothing by chain, so
  reusing the retired network's volume leaves its blocks and miners in the
  tables beside Aglais data. The rename gives a clean index on upgrade and
  deletes nothing: the old volume stays until the operator removes it.
- `config/advantage2-system1-h0.spec.json` holds the topology the network
  mines against, `0xe66d...a02d`. It is the Advantage2 system1 graph with
  `allowed_h = [0]`, the h0 variant the previous testnet registered. The
  built-in `advantage2-system1` preset differs, carrying
  `allowed_h = [-1000, 0, 1000]`, so `seed-chain` run with its defaults
  registers a different topology. README documents the seeding command.

**Operator impact**: `git pull`, `make updateconfig`, drop the dashboard state,
`docker compose --profile cpu up -d`. See "Upgrading to Aglais" in the README.
The old database stays at `data/validator-data` until you delete it.

### The miner, dashboard, and faucet wait for a synced validator

`quip-validator` now carries a healthcheck, and the three services that read the
chain through it moved from `condition: service_started` to
`condition: service_healthy`.

All three give wrong answers against a validator that is still catching up. The
miner's coordinator reads the runtime at the validator's best block, so a node at
genesis reports the genesis runtime and the preflight exits with `validator runtime
quip/103 exposes QuantumPowApi v1, but this coordinator drives v2`. That message
names an upgrade, but the validator does not need one: it needs to finish syncing.
Before this change the coordinator crash-looped for the length of the initial sync.
The dashboard indexer scans from genesis and cached an empty chain as the network.
The faucet cannot submit transfers against a node that has not reached the head.

`scripts/validator-healthcheck.sh` is the check. It reads the `system_health` RPC
over bash's `/dev/tcp`, because the node image ships no HTTP client. Healthy means
not major-syncing, and either connected to peers or running a chain that expects no
peers. The second arm is what keeps `make localdev` working, where Alice authors
alone on `--chain=dev` and the peer count stays 0 forever.

`start_period` is `24h`. Docker marks a container unhealthy for good after
`retries` failures, and the gated services would then never start.

**Operator impact**: a fresh testnet install holds on `docker compose up -d` until
the initial sync completes, which takes hours under the archive pruning the stack
sets. Watch it with `docker compose logs -f quip-validator`. Later starts return at
once. A validator that stays at block 0 with 0 peers is on the wrong genesis, not
mid-sync. Pass `chain-specs/quip-testnet.json`, whose genesis is `0xa139…a9a7`. Do
not pass the binary's built-in `quip-testnet` preset. That preset is a different
genesis that no public node runs.

### Localdev resolves the newest image tags from the registry

`make localdev` no longer runs whatever `latest` points at. It calls `scripts/newest-tags.py`, which asks the GitLab registry which tag each quip image published most recently. The script writes the answers to `data/localdev.tags.env`, which compose reads after `.env`.

CI does not move `latest` for rc builds, so a localdev stack that tracked `latest` ran an old build without saying so. The validator is the clearest case: `latest` and `v0.2` are the same digest, node `0.2.1`, runtime `specVersion` 112, while the newest tag `v0.2.2-rc5` carries `specVersion` 115.

Newest means most recently published. The four repositories use different tag schemes, so publish time is the one ordering that covers them all. CI publishes a version tag, a `sha-` commit alias, and sometimes `latest` for a single build, within the same second. The resolver groups tags by digest and reports the most descriptive name on the winning image, preferring `v0.2.2-rc6` over the `sha-d1783ed8` alias for the same bytes.

Pins still win. The resolver passes through a `QUIP_*_TAG` set in `.env` or the environment, and skips the lookup for that image.

A registry outage does not stop a localdev run. When a lookup fails, the resolver reuses the tags from the previous run, or falls back to `latest` when there is no previous run. It reports what it did on stderr and exits 0.

The script reads the GraphQL API, which sorts tags by publish time on the server. The whole resolve costs two requests and about six seconds. Auth is anonymous, so any operator can run it as is.

`make testnet` still tracks `latest`. A release pointer is what a production stack wants.

### Image tags default to `latest`

Every quip image now defaults to `latest` instead of a pinned version:

| variable | was | now |
|---|---|---|
| `QUIP_MINER_TAG` | `v0.3.0-rc7` | `latest` |
| `QUIP_VALIDATOR_TAG` | `v0.2` | `latest` |
| `QUIP_DASHBOARD_TAG` | `v0.2` | `latest` |
| `QUIP_FAUCET_TAG` | `latest` | `latest` |

Each service already sets `pull_policy: always`, so `docker compose up` re-resolves the tag on every start. An operator who sets no tag runs the newest published build.

`env.example` no longer assigns these variables. It comments out each one and shows the pin syntax on the preceding line. The variables still work, so set one to pin a deploy to a release or a `sha-XXXXXXXX` commit tag.

`postgres:16` and `caddy:2-alpine` stay pinned. Both are third-party images where a major version jump can break the on-disk data format.

The `docker run` commands in `README.md` and `docs/testnet-deployment.md` now use `:latest` for the validator image.

**Operator impact**: a `QUIP_*_TAG` line in an existing `.env` overrides the compose default, so a stale pin holds the stack on an old build. Delete these lines from `.env` to track `latest`. A pin left at `v0.2` against the v0.3 miner path fails the pull with `manifest unknown`.

### Miner services moved to the v0.3 coordinator images

The `cpu` and `cuda` services now pull from the v0.3 repository line:

| service | was | now |
|---|---|---|
| `cpu` | `quip-miner/quip-miner-cpu` | `quip-miner/v0.3/quip-miner` |
| `cuda` | `quip-miner/quip-miner-cuda` | `quip-miner/v0.3/quip-miner-cuda` |

The v0.3 images are a separate repository line, not new tags on the old one. The old repositories stop at `v0.2.1-rc54`, so a `QUIP_MINER_TAG` set to a v0.3 tag against the old path fails the pull with `not found`. Note that the CPU image dropped its `-cpu` suffix, because the coordinator now bundles every miner binary it supports.

`QUIP_MINER_TAG` defaults to `latest` on the new path.

### Chain seeding moved into the coordinator

`scripts/seed-advantage2-topology.py` is deleted. The v0.3 miner image dropped the Python chain-interaction modules the script imported (`dwave_topologies`, `shared.hybrid_signer`, `substrate.client`, `substrate.miner_bootstrap`), so the script could not run inside the image.

`quip-coordinator seed-chain` replaces it. The coordinator carries the `advantage2-system1` topology in the binary, so the localdev seed step no longer mounts a script:

```bash
docker compose -f docker-compose.yml -f docker-compose.localdev.yml \
  --profile cpu run --rm \
  --entrypoint quip-coordinator cpu seed-chain \
  --validator ws://quip-validator:9944 --sudo-key //Alice
```

`--sudo-key` takes a dev URI, a BIP39 mnemonic, a 32-byte hex master seed, or a keystore path. `--mnemonic-file` takes a path to a BIP39 phrase.

**Operator impact**: a fresh chain still needs seeding before any miner can submit a proof. The v0.3 coordinator does not exit when `DefaultTopology` is unset. It stages no work and logs `feeder: chain has no mining snapshot (no registered/mineable topology)`.

### First-run config templates removed

`config/quip-miner.{cpu,cuda}.toml` are deleted and no longer bind-mounted. Two reasons:

- The v0.3 entrypoint seeds from `/app/config.toml`, not `/app/quip-miner.docker.toml`, so the mount had no effect.
- The templates carry the v0.2 miner schema, which the v0.3 coordinator rejects.

Each image ships its own `/app/config.toml` and seeds `data/config.toml` from it on first run. To customise the config, edit `data/config.toml` and restart.

**Operator impact**: an existing `data/config.toml` in the v0.2 schema does not start under v0.3. The coordinator needs a backend section (`[cpu]`, `[cuda.N]`, `[metal]`, or `[dwave]`) and both `[miner].public_host` and `[miner].public_port`. It ignores `[miner].rest_host` and `[miner].rest_port`. The REST surface that Caddy proxies at `/api/v1/*` now comes from `[dashboard].listen`, which needs `data_dir` set as well or the dashboard stays off.

### Localdev configs updated for the v0.3 schema

`config/localdev.{cpu,cuda}.toml` replace `[miner].rest_host` and `rest_port` with a `[dashboard]` section on the same port 8086, and add the `public_host` and `public_port` keys the coordinator requires.

## v0.2.1

The last release on the retired testnet. Operators who stay on v0.2.x keep
that network. Aglais starts at v0.3.0-rc1.

### CPU miner `shm_size` raised to avoid SIGBUS

The `cpu` miner service now sets `shm_size: "2gb"`. Each SA worker streams samples through a POSIX shared-memory ring (~75 MiB per worker at the Advantage2 topology's maximum reads), which overflows Docker's 64 MiB `/dev/shm` default. Because tmpfs is sparse, the allocation succeeds and the miner instead dies with **SIGBUS (`exitcode=-7`)** when a worker first writes an unbackable page — reported on a 12-core host, but the default single-CPU config exceeds 64 MiB too. The cap is not a reservation (tmpfs pages are consumed only when written), so the generous value is free. The `cuda` service is unaffected: it already sets `ipc: host`, sharing the host's `/dev/shm`. Matches quip-miner v0.2.1-rc45.

**Operator impact**: none for compose users — the new default applies on `docker compose up`. Operators running the miner image directly with `docker run` must add `--shm-size=2g` themselves.

### Miner is config-driven — `QUIP_*` miner env vars removed

The quip-miner v0.2.1-rc miner images (which the rolling `:v0.2` registry tag serves) dropped every configuration env var: `data/config.toml` is the single source of truth, and the entrypoint's env contract is `PUID`/`PGID` only. This repo now matches that contract:

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
