# Quip Network Node - Docker Deployment

> **Aglais relaunch (2026-09-02).** The Quip test network restarted from a fresh genesis as **Aglais**. This repo joins Aglais by default. Existing operators: see [Upgrading to Aglais](#upgrading-to-aglais).

> **Upgrading from v0.1?** See [Upgrading from v0.1](#upgrading-from-v01) below — the miner config schema changed, container/image names changed, and the substrate validator now owns p2p. There's a one-time `make updateconfig` step plus a few docker compose differences worth scanning before you bring things up.

Quick-start Docker Compose deployment for Quip Network nodes. It supports CPU and CUDA (GPU) mining, with an optional Substrate-based validator and faucet sidecar. Each profile brings up the telemetry dashboard, which runs Caddy and its own embedded database, for a single-URL monitoring UI with automatic TLS out of the box.

## Architecture

```
Internet
  ├─ 80/tcp  → quip-dashboard (Caddy; TLS mode: auto-redirect to :443)
  ├─ 443/tcp → quip-dashboard (Caddy) ─┬─ /rpc/*        → quip-validator:9944  (substrate RPC)
  │                                    ├─ /api/faucet/* → quip-faucet:8087     (faucet profile)
  │                                    ├─ /api/v1/*     → quip-miner:8086      (miner telemetry)
  │                                    ├─ /files/*      → indexer data files
  │                                    └─ /*            → dashboard SPA + API
  ├─ 20049/tcp → quip-dashboard (same routes as :443; the canonical Quip API port)
  └─ 30333/tcp+udp → quip-validator (substrate libp2p, bundled into every profile)
```

The stack runs three images: the validator, the miner, and the dashboard. The dashboard container runs Caddy, the syslog-ng log collector, and the dashboard backend under one supervisor. It stores its index in an embedded Turso database at `dashboard-data/dashboard.db`.

Caddy is the single front door for HTTP/WS traffic; libp2p binds `:30333` directly on the validator container. The miner runs purely as an outbound substrate RPC client (no inbound QUIC, no inbound REST) and is reachable only over the compose network. Substrate RPC is at `/rpc`, faucet at `/api/faucet/*`, miner telemetry at `/api/v1/*`, dashboard SPA at `/`. All four are served on **both** `:443` and `:20049` in TLS mode, or on `:20049` HTTP-only in dev mode. `:80` is used for ACME HTTP-01 challenges and auto-redirects to `:443`.

**Inbound ports** every public deployment should open: `:20049` (Caddy — required for the dashboard/RPC/telemetry surface), `:30333/tcp+udp` (libp2p peer dials — strongly recommended so other validators can connect inbound and your node can be a useful peer), and `:80`+`:443` (Caddy TLS — only if you want HTTPS via HTTP-01 ACME). Without `:30333`, your validator still mines and gossips outbound through the bootnodes, but won't accept inbound peer dials — you become a leaf in the mesh rather than a participating peer.

Verify your ports are open from the public internet via [`check.quip.network`](https://check.quip.network) — run the curls from the host itself; the service uses the caller's source IP so you can't misdirect it at a different target:

```bash
curl -sS https://check.quip.network/checkport?port=20049
curl -sS https://check.quip.network/checkport?port=30333
curl -sS https://check.quip.network/checkport?port=80    # only if using HTTP-01 ACME
curl -sS https://check.quip.network/checkport?port=443   # only if using HTTPS
```

Each returns `{"reachable": true, …}` or `{"reachable": false, "error": "…"}`.

## Upgrading to Aglais

Aglais is the Quip test network, in the way Sepolia is the Ethereum test network. It replaced the previous testnet on 2026-09-02 with a fresh genesis: runtime `117`, transaction version `7`, token `AGLS`. The previous testnet still runs while operators move, and it will be retired. This repo joins Aglais by default.

What changed on the host:

- `chain-specs/aglais-network.json` replaces `chain-specs/quip-testnet.json`. Genesis is `0x59e064bddd49a920d1392693c728c3bf9867f2cf3e0f8fea8c2498b389b0c286`. Bootnodes are `bootnode-{1,2,3}.aglais.quip.network`.
- The validator base path moved from `data/validator-data` to `data/aglais-chain-db`. Aglais keeps the chain id `quip_testnet`, so the new spec would open the old database and reject it on genesis mismatch. A fresh directory keeps the two apart.
- The validator image is pinned to the Aglais build by the `BETA` channel. `latest` still points at the pre-Aglais image, which cannot run this chain. Run `make show-channel` to see the exact tag.
- The faucet is `https://faucet.aglais.quip.network`. The miner funds and registers itself again on the new chain, so `faucet_url` in `data/config.toml` must point at the Aglais faucet. Fresh installs get it from `config/quip-miner.toml`, which compose mounts over the miner image's first-run template.
- The dashboard Postgres volume is `aglais-pgdata`, renamed from `quip-pgdata`. The indexer scans from genesis and keys nothing by chain, so the retired network's blocks and miners would otherwise sit in the tables beside Aglais data. The new name gives a clean index without deleting anything.

Steps:

```bash
git pull
make updateconfig                    # rewrites faucet_url to the Aglais faucet
docker compose --profile cpu down    # or cuda
docker compose --profile cpu up -d   # or: make testnet PROFILE=cpu
```

`up -d` creates `data/aglais-chain-db` and syncs Aglais from genesis. The first start waits for that sync, see [Initial sync](#initial-sync).

The miner keystore at `data/keystore.json` still loads, but the miner image the `BETA` channel selects signs with the H4 suite and derives a different on-chain account from the same seed. The miner funds and registers that new account itself on first start against Aglais, through `faucet_url`. The `ss58` field inside `keystore.json` is stale metadata from the H3 era and is no longer the account the miner uses. Read the live account from the dashboard or from `QuantumPow.Miners`, not from the file.

Nothing from the retired network is deleted for you. The old chain database stays at `data/validator-data` and the old dashboard index stays in the `quip-pgdata` volume. Delete both when you no longer need to go back:

```bash
trash data/validator-data          # or: rm -rf data/validator-data
docker volume rm quip-pgdata
```

If `.env` pins `QUIP_VALIDATOR_TAG` to a pre-Aglais tag, delete the line. A pin holds the stack on an image that cannot run this chain.

Bootnode operators: copy `node-key` into the new base path and insert the rotated session keys. See [docs/testnet-deployment.md](docs/testnet-deployment.md).

To keep a node on the previous testnet, stay on a checkout from before this change.

### Upgrading to the embedded dashboard

The dashboard image now runs Caddy, the log collector, and its own database. The `caddy`, `postgres`, and `quip-syslog` services are gone. After you pull this version, run `up` once with `--remove-orphans`. The old containers otherwise keep ports 20049, 80, 443, and 5514 and the dashboard cannot start:

```bash
docker compose --profile cpu up -d --remove-orphans
```

`make testnet` and `cron.sh` pass `--remove-orphans` for you. Watchtower does not add or remove services, so a node that only Watchtower updates needs this command once.

The dashboard rebuilds its index from genesis into `dashboard-data/dashboard.db`. Existing TLS certificates carry over. When the new dashboard is up, delete the old Postgres volume:

```bash
docker volume rm aglais-pgdata
```

## Upgrading from v0.1

The v0.2 stack replaces the v0.1 P2P mesh with a substrate validator + RPC-client miner. That changes the binary (`quip-node` → `quip-miner`), the image names (`quip-network-node-{cpu,cuda}` → `quip-miner-{cpu,cuda}`), the compose services (`qpu` collapsed into `cpu` + a `[qpu]` config section; new `quip-validator` and `quip-faucet` services), and the config schema (`[global]` → `[miner]`, P2P/TLS keys removed, `validators` + `signer_key` required). Full schema diff in [CHANGELOG.md](CHANGELOG.md).

### 1. Stop and remove the v0.1 containers

The v0.1 container names (`quip-cpu`, `quip-cuda`, `quip-qpu`, `quip-dashboard`, `quip-postgres`, `quip-caddy`) still exist in v0.2 except for `quip-qpu`, so a plain `docker compose down` from the new tree won't necessarily reach them if you've already pulled v0.2. Stop and remove them explicitly first — `|| true` makes this safe to copy/paste even if some containers don't exist on your host:

```bash
docker stop quip-cpu quip-cuda quip-qpu quip-dashboard quip-postgres quip-caddy 2>/dev/null || true
docker rm   quip-cpu quip-cuda quip-qpu quip-dashboard quip-postgres quip-caddy 2>/dev/null || true
```

Your data is in bind mounts (`./data/`, `./dashboard-data/`) and volumes (`quip-caddy-data`, `quip-caddy-config`, plus `aglais-pgdata` if you have not yet deleted the pre-embedded dashboard's Postgres volume), so removing containers is non-destructive.

### 2. Pull the v0.2 repo

```bash
git pull origin v0.2
```

Review `docker-compose.yml` and `env.example` against your local `.env`:

- **New env vars** you'll want to set before the first start: `QUIP_MINER_CPUSET`, `VALIDATOR_NAME`, `CERT_EMAIL`.
- **Image tag vars** (`QUIP_MINER_TAG`, `QUIP_VALIDATOR_TAG`, `QUIP_DASHBOARD_TAG`, `QUIP_FAUCET_TAG`) are unset by default. `CHANNEL` names the tag instead, and every image publishes both `beta` and `stable`. Both channels run Aglais: `stable` is the released line, `beta` the prerelease line ahead of it. The validator publishes no non-rc v0.3 build yet, so its two channels resolve to the same Aglais image until one exists. Set a tag var only to pin an exact version, and note that a stale pin holds the stack on an old build. `make show-channel` prints what you will actually pull.
- **Removed env vars** — delete these from your `.env` if present (they're no longer consumed by v0.2 and only clutter the file):
  - `QUIP_NODE_URL` — superseded by `QUIP_VALIDATOR_RPC_URLS` (now drives both chain indexing and the miner REST surface; comma-separated list of substrate WS URLs).
  - `QUIP_NODE_TOKEN` — removed; bearer-token access control moved out of the dashboard image into the deployment layer (reverse-proxy auth, network policy).
- **Repointing for miner-only nodes**: if your `.env` had `QUIP_NODE_URL=https://cpu-1.nodes.quip.network` (or similar single-host), the v0.2 equivalent is `QUIP_VALIDATOR_RPC_URLS` pointing at the same host's substrate WS endpoint (comma-separated if you want failover across multiple validators):
  ```
  QUIP_VALIDATOR_RPC_URLS=wss://cpu-1.nodes.quip.network/rpc
  ```

### 3. Convert `data/config.toml`

> ⚠️ **First, make sure your shell user can move every file in `data/`.** The converter moves the v0.1 contents into `data/.v0.1_backup/` and will fail with a `PermissionError` if any file is owned by a different user (commonly the case if your v0.1 node ran the container as root). Run this once before the converter:
>
> ```bash
> sudo chown -R "$(id -u):$(id -g)" data/
> ```
>
> Skip if your `data/` is already owned by your shell user (e.g., you've been running v0.1 with `PUID=$(id -u)`).

Pick one (both produce identical output):

```bash
# Native — needs Python 3.11+ on the host
make updateconfig

# Docker fallback — for Python < 3.11 (e.g. Ubuntu 22.04 ships 3.10)
make updateconfig-docker

# Or call the script directly
python3 scripts/upgrade-config.py data
```

Defaults to `./data`; override with `DATA=/path/to/data`. The converter:
- moves every entry in `data/` (including your old `config.toml`) into `data/.v0.1_backup/`
- writes a fresh `data/config.toml` in v0.3 shape, carrying over `node_name`, `public_host`, `public_port`, `log_level`, `node_log` and preserving backend tables (`[cpu]`, `[gpu]`, `[cuda.N]`, `[qpu]`, `[dwave]`, …) verbatim
- moves the REST surface into `[dashboard]`. v0.3 removed `[miner].rest_host` and `[miner].rest_port`, and the coordinator names both keys when it rejects a config. The converter pins `listen` to `0.0.0.0:8086` because the dashboard image's Caddyfile proxies `/api/v1/*` to `quip-miner:8086`. It carries no v0.1 value over: those deployments often set `rest_port = 443` so the miner served TLS itself, which leaves Caddy's upstream unreachable
- adds `binary = "quip-cpu-sa"` to a `[cpu]` table that has none. v0.3 selects the miner variant with this key
- warns when a QPU (`[dwave]`/`[qpu]`) is the only backend. A QPU rejects every job while its access-time budget is spent, and the coordinator drops a rejected job when nothing else can take it, so such a node mines nothing between refills. Adding `[cpu]` absorbs the rejections
- warns when the config names no mining backend at all. v0.3 refuses to start without one of `[cpu]`, `[cuda.N]`, `[metal]`, `[dwave]`/`[qpu]`, and the converter reports this rather than choosing your hardware for you
- defaults `validators = ["ws://quip-validator:9944", "ws://127.0.0.1:9944"]` — the bundled local validator, then a host-network fallback. These match the coordinator's own built-in default. The first entry peers with the Aglais bootnodes via libp2p on `:30333`, so the pair is correct out of the box and does not need editing
- sets `faucet_url = "https://faucet.aglais.quip.network"` so the miner's first-boot self-bootstrap (register + fund) works
- defaults `signer_key = "/data/keystore.json"` — the entrypoint auto-generates the hybrid keystore on first start
- warns loudly about dropped `[global].port` / `[global].listen` (semantics flipped from QUIC peer to telemetry REST — the v0.2 loader would silently alias these, but that risks exposing the REST API on what used to be the peer port)

Already on the `[miner]` schema? Run it anyway. On such a config the converter backfills the keys that used to arrive through the now-removed `QUIP_*` env vars (`validators`, `faucet_url`), harvesting any uncommented values from your `.env` before stripping those dead lines. It then applies the v0.2 to v0.3 migration in the same pass: the rest keys move into `[dashboard]`, `[cpu].binary` is filled in, and a `faucet_url` still naming the retired testnet is repointed at Aglais. Backups are `data/config.toml.pre-backfill.bak` and `.env.pre-config-driven_backup`.

[`config/config.example.toml`](config/config.example.toml) documents every key the converter writes.
- migrates `.env` alongside (sibling of `data/`): backs up the current file to `.env.v0.1_backup`, drops stale `QUIP_NODE_URL` / `QUIP_NODE_TOKEN` lines (commented or uncommented), and appends a commented `QUIP_VALIDATOR_RPC_URLS` placeholder. Use `--no-env-file` (or `make updateconfig DATA=data` with the env override unset) to skip the `.env` step.

Idempotent: re-running on an already-converted dir exits with "nothing to do".

### 4. Decide on the ACME challenge type

Caddy auto-provisions a Let's Encrypt cert for `QUIP_HOSTNAME` in production mode. Two ways it can prove control of the DNS name:

| Challenge | What you need | When to pick |
|---|---|---|
| **HTTP-01** (default) | Port **80** reachable from the public internet | Simplest. Works out of the box with the dashboard image. Required if you can't or won't share DNS API credentials with the host. |
| **DNS-01** | A dashboard image built with your DNS provider's Caddy plugin compiled in (`caddy-dns/cloudflare`, `caddy-dns/route53`, `caddy-dns/digitalocean`, …) and DNS-API credentials wired into the `dashboard` service via a `docker-compose.override.yml` `environment:` entry (values can live in `.env`, but must be wired through — `.env` alone does not reach containers) | Required if your host cannot bind `:80` (firewalled, port already taken, behind a NAT without port-forward). Also supports wildcard certs. |

For HTTP-01, no extra config — just make sure `:80` is open and `CERT_EMAIL` is set in `.env`. For DNS-01, build a dashboard image with your provider's Caddy plugin compiled in (see [Caddy's DNS challenge docs](https://caddyserver.com/docs/automatic-https#dns-challenge)), and add the appropriate `tls { dns <provider> }` block to the Caddyfile you mount over `/etc/caddy/Caddyfile` in `docker-compose.override.yml`. The plumbing is out of scope for this repo because the credential surface is provider-specific.

### 5. Bring the v0.2 stack up

```bash
# CPU miner + bundled local validator + dashboard
docker compose --profile cpu up -d

# CUDA miner + bundled local validator + dashboard
docker compose --profile cuda up -d

# Layer in the faucet (dev only)
docker compose --profile cpu --profile faucet up -d
```

Both `cpu` and `cuda` profiles bundle a local substrate validator by default — there's no separate validator profile anymore. The first start pulls the latest images (`quip-miner`, `quip-miner-cuda`, `quip-network-node`, `quip-faucet`, and the dashboard) and auto-generates `data/keystore.json` for the miner. The miner self-bootstraps on startup: it funds the new account via the Aglais faucet (`https://faucet.aglais.quip.network`, set as `faucet_url` in the seeded `data/config.toml`) and registers it in the chain's `QuantumPow.Miners` map before it starts producing proofs. Comment out `faucet_url` in `data/config.toml` (and restart) to opt out of faucet-funding if you've pre-funded the account yourself.

Check it came up cleanly:

```bash
docker compose --profile cpu ps
docker compose logs -f cpu              # miner
docker compose logs -f quip-validator   # validator
```

Then visit the dashboard at `http://localhost:20049/` (dev) or `https://<your-hostname>/` (production).

### CUDA / GPU mining and NVIDIA MPS

For hardware SM sharing across miner processes the CUDA miner uses [NVIDIA MPS](https://docs.nvidia.com/deploy/mps/) (Multi-Process Service). MPS is a **host** facility — a control daemon runs on the host and exposes a pipe directory (`/tmp/nvidia-mps`) that the container joins via `ipc:host`. Without it the miner logs `MPS not active in container — using software nonce reduction only` and runs in a degraded fallback; it does **not** fail. MPS is unsupported under WSL2 / Docker Desktop.

`make testnet PROFILE=cuda` starts the host MPS control daemon for you (the `require-mps` target) before bringing the stack up, so SM sharing just works:

```bash
make testnet PROFILE=cuda
```

The `cuda` service in `docker-compose.yml` is already wired for this (`ipc:host`, `pid:host`, the `/tmp/nvidia-mps` bind-mount, and the `CUDA_MPS_*` env). If you bring the stack up with raw `docker compose --profile cuda up -d` instead of `make`, start the daemon on the host yourself first (may require root):

```bash
sudo nvidia-cuda-mps-control -d
```

Set `QUIP_GPU_UTILIZATION` in `.env` to cap each miner's GPU SM share (`CUDA_MPS_ACTIVE_THREAD_PERCENTAGE`); default `100`, use `100/N` for `N` miners sharing one GPU. If the NVIDIA driver's MPS utilities aren't installed, `make testnet PROFILE=cuda` warns and continues with the software fallback.

### Rollback

If you need to undo the conversion: restore the original `data/config.toml` from the backup and remove the v0.2 file.

```bash
cp data/.v0.1_backup/config.toml data/config.toml
```

The v0.1 containers can be re-created from a v0.1 checkout of this repo (`git checkout main` if `main` still points at the v0.1 line, or `git checkout <pre-v0.2-sha>`). Bind-mounted data survives across both stacks.

## Setup

### 1. Choose a mode and copy the config template

```bash
# CPU mining (also the base for QPU/D-Wave — see step 2)
cp data/config.cpu.toml data/config.toml

# CUDA GPU mining (requires NVIDIA GPU + drivers)
cp data/config.cuda.toml data/config.toml
```

### 2. Configure the node

Edit `data/config.toml`:
- Adjust `node_name` for telemetry display
- For QPU (D-Wave): uncomment the `[qpu]` and `[dwave]` sections at the bottom of `config.cpu.toml`. Solver and budget are pre-set for Advantage2.

`[miner].validators` defaults to `ws://quip-validator:9944` — the bundled local validator. Leave it alone; it peers with the canonical testnet bootnodes via libp2p (see the chain spec's `bootNodes`).

### 3. Configure credentials

```bash
cp env.example .env
printf 'PUID=%s\nPGID=%s\n' "$(id -u)" "$(id -g)" >> .env
```

Then edit `.env` and set `QUIP_HOSTNAME`:

| Mode | `QUIP_HOSTNAME` value | What you get |
|---|---|---|
| Dev / local | `:20049` (default) | HTTP on `:20049` only, no TLS. |
| Production | `cpu-1.nodes.quip.network, cpu-1.nodes.quip.network:20049` | Auto-TLS on `:443` + `:20049`, `:80` auto-redirects to `:443`. |

The comma-separated production form is required so a single Let's Encrypt cert covers both ports. Port 80 must be reachable from the internet during cert provisioning and every renewal.

Also set:
- `CERT_EMAIL` — required when running in TLS / production mode.
- `DWAVE_API_TOKEN` — required only for QPU / D-Wave mining. Set `DWAVE_API_SOLVER` too on a real QPU: without it the Ocean SDK picks your account default, which may not be the Advantage2 system the chain topology targets. `DWAVE_API_KEY` is the old name and still maps forward, but nothing reads it directly.
- `QUIP_VALIDATOR_TAG`, `VALIDATOR_NAME` — see `env.example` for the validator and faucet sections.

The `printf` line seeds `.env` with your host's uid/gid so files under `./data/` stay editable without `sudo`. Since quip-miner v0.1.7 the node runs as a non-root `quip` user and chowns `/data` to match `PUID`/`PGID` on start (default 1000). Run the `printf` line as the non-root user who owns the checkout: a root shell writes `PUID=0`, and the dashboard image refuses that value and exits.

> **Note:** `.env` is docker compose's interpolation source, not a blanket container env file. A variable only reaches a container when `docker-compose.yml` explicitly wires it through an `environment:` entry — custom variables you add to `.env` are invisible to containers unless you also wire them via a `docker-compose.override.yml`.

### 4. (Recommended) Tune the host kernel

Apply BBR + fair-queueing + no slow-start-after-idle on the host — improves throughput for long-lived TCP and is required for BBR's packet pacing:

```bash
sudo ./scripts/sysctl-tune.sh
```

Idempotent. Writes `/etc/sysctl.d/99-quip.conf` and runs `sysctl --system`. Needs kernel ≥ 4.9 (every supported Ubuntu LTS qualifies).

### 5. Start

Two primary profiles are available. Plain `docker compose ...` always boots the **live Quip Testnet** — the dev-chain override is opt-in (see [Local dev chain](#local-dev-chain) below).

| Profile | Includes | Notes |
|---|---|---|
| `cpu` | miner (CPU), local validator, dashboard | Default. Uncomment `[qpu]` + `[dwave]` in `config.toml` for D-Wave. |
| `cuda` | miner (CUDA), local validator, dashboard | Requires NVIDIA GPU + Docker GPU runtime. |

Every node bundles its own substrate validator — there's no separate validator-only or miner-only profile.

```bash
# CPU miner + local validator (testnet)
docker compose --profile cpu up -d

# CUDA miner + local validator (testnet)
docker compose --profile cuda up -d
```

#### Initial sync

The first testnet start waits for the validator to sync. `up -d` holds until the
validator reaches the chain head. On a fresh install this takes hours, because the
stack runs an archive node and replays every block from genesis. Later starts return
at once, because the node keeps its database in `data/aglais-chain-db`.

The miner and the dashboard wait on purpose. Both read the chain through the
validator, and both give wrong answers against a node that is still catching up:

- The miner's coordinator reads the runtime at the validator's best block. A node at
  genesis reports the genesis runtime, so the coordinator exits with
  `validator runtime quip/103 exposes QuantumPowApi v1, but this coordinator drives
  v2 — upgrade the validator`. The validator does not need an upgrade. It needs to
  finish the sync.
- The dashboard indexer scans from genesis. A validator at genesis gives it an
  empty chain, which it caches as the network.

Watch progress from a second terminal:

```bash
docker compose --profile cpu logs -f quip-validator
docker compose --profile cpu ps          # quip-validator: (health: starting) -> (healthy)
```

A syncing node reports its target block and its peer count:
`⚙️ Syncing 234.6 bps, target=#1353316 (4 peers)`.

A node that stays at block 0 with 0 peers is on the wrong genesis. Check that
`--chain` points at `chain-specs/aglais-network.json`, whose genesis is
`0x59e0…c286`, and that `data/aglais-chain-db` does not hold a database from
another chain. Pass the file, not a preset name.

#### Local dev chain

For experimentation without joining the testnet — `//Alice` as the sole authority + sudo + faucet funder. Two ways:

```bash
make localdev                           # full flow: wipe + start + seed topology + bootstrap miner
# or
docker compose -f docker-compose.yml -f docker-compose.localdev.yml --profile cpu up -d
```

The opt-in `docker-compose.localdev.yml` swaps the validator command to `--chain=dev` and pulls `quip-faucet` into the `cpu`/`cuda` profiles; `make localdev` also copies `config/localdev.<profile>.toml` to `data/config.toml` so the miner's `faucet_url` points at the local faucet. **It is not auto-loaded** — the filename was deliberately moved off the magic `docker-compose.override.yml` to keep plain `docker compose` invocations on the testnet path. If you previously relied on the override auto-applying, switch to `make localdev` or pass the explicit `-f` flags.

**Monitor your node at [http://localhost:20049/](http://localhost:20049/)** — or `https://<QUIP_HOSTNAME>/` (and `:20049`) when running on a remote machine with TLS.

`cron.sh` detects which profiles are running (based on which `quip-*` containers exist) and preserves them on auto-update.

### TLS

With the default `QUIP_HOSTNAME=:20049`, Caddy serves HTTP on port 20049 with no TLS — good for local dev with the `cpu` / `cuda` profiles. Access the dashboard at `http://localhost:20049/`.

For production, set `QUIP_HOSTNAME` to the comma-separated form (`example.com, example.com:20049`) and `CERT_EMAIL` to a valid address. Caddy provisions a Let's Encrypt cert via HTTP-01 on `:80`, serves HTTPS on `:443` and `:20049`, and redirects HTTP to HTTPS. Port 80 must be reachable from the internet during provisioning and every renewal — if it isn't (firewalled host, port already taken, NAT without port-forward), see the [DNS-01 alternative](#4-decide-on-the-acme-challenge-type) in the upgrade flow.

The default ACME issuer is **Let's Encrypt**, with **ZeroSSL** as an automatic fallback (built-in to Caddy 2.6+). To pin ZeroSSL as the primary issuer — useful if you want longer cert validity or have hit LE rate limits — mount a Caddyfile that sets `cert_issuer zerossl` over `/etc/caddy/Caddyfile` in `docker-compose.override.yml`. Start from `deploy/Caddyfile` in the dashboard repository, and optionally set `ZEROSSL_API_KEY` in `.env` for pre-provisioned EAB credentials.

Certs persist in the `quip-caddy-data` named volume across container recreations.

### Dashboard

The dashboard polls the local validator (`ws://quip-validator:9944`) and the local miner (`http://quip-miner:8086`) over the compose network. The node's RPC is **not** exposed to the host directly. All external traffic goes through Caddy in the dashboard container.

For miner-only nodes (no colocated validator), point the indexer at a public full node via `QUIP_VALIDATOR_RPC_URLS` in `.env`. The value is comma-separated; the indexer rotates through the list on failure:

```bash
QUIP_VALIDATOR_RPC_URLS=wss://cpu-1.nodes.quip.network/rpc
```

The index persists in `dashboard-data/dashboard.db`, so it survives container recreations. While the validator is still syncing, the indexer waits and does not index a partial chain.

### Validator setup

Every node bundles a local substrate validator, so just starting the `cpu` or `cuda` profile makes you a validator. What changes by deployment:

- **Inbound `:20049`** (Caddy) — required for the dashboard/RPC surface to be reachable from the public internet.
- **Inbound `:30333/tcp+udp`** (libp2p) — same priority as `:20049`. Lets other validators dial yours so you're a participating peer instead of a leaf. Mining works without it (outbound to bootnodes is enough), but your peer count stays in the single digits.
- **TLS for the public RPC** is best-effort and Caddy-driven. Set `QUIP_HOSTNAME` to your real DNS name + `CERT_EMAIL`, open port 80 (HTTP-01) or wire DNS-01, and Caddy serves `wss://<host>/rpc` and `wss://<host>:20049/rpc`. Without those, the validator still runs locally and is reachable on the compose network (`ws://quip-validator:9944`) — only the public WSS endpoint is gated on the cert.
- **Docker Compose v2.20+** for the `depends_on.required: false` flag used to make the validator a soft dependency of the miner. On older versions, remove that line from `docker-compose.yml` (miner will retry RPC connection on startup either way).

After bringing the stack up, verify `:20049` and `:30333` are reachable from the public internet via [`check.quip.network`](https://check.quip.network) — see the [architecture section](#architecture) for the exact curl invocations.

The validator boots against `chain-specs/aglais-network.json` by default (see [Aglais](#aglais-the-quip-test-network) below). For a self-contained dev chain, use `make localdev` (see [Local dev chain](#local-dev-chain)).

```bash
# 1. Start the stack — chain spec is already in place
docker compose --profile cpu up -d

# 3. Rotate session keys — substrate generates and stores them under the
#    keystore in ./data/aglais-chain-db/chains/<id>/keystore/. The returned
#    hex pubkey gets bound to your validator account via session.setKeys.
curl -fsSL -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"author_rotateKeys","params":[]}' \
     https://<QUIP_HOSTNAME>/rpc

# 4. Submit session.setKeys from your controller account using the pubkey
#    above, via Polkadot.js Apps pointed at wss://<QUIP_HOSTNAME>/rpc.
```

Notes:
- `--rpc-methods=safe` blocks `author_rotateKeys` from external callers as a hardening default. Run step 3 from inside the docker network (e.g. `docker compose exec quip-validator …` with the substrate node's curl) if your remote `/rpc` blocks the call.
- Running **two validators on one host** is not supported — the upstream litep2p transport's wildcard binding causes a port collision when multiple validators share a docker bridge. Use separate hosts or separate docker networks.
- **The validator database grows without bound, and that's the supported configuration.** It runs with `--state-pruning=archive --blocks-pruning=archive`, keeping every state trie and block body from genesis. Size the disk for that. You can reclaim disk by overriding both flags in a `docker-compose.override.yml`, but pruning breaks the dashboard: its descriptor worker scans from genesis and fails with `State already discarded` once it reads past the pruning window. A pruned node isn't a recommended configuration and likely reduces your point awards on the SNAG platform. Only prune if you accept losing the dashboard.
- The validator's libp2p node key is auto-generated at first start (under `data/aglais-chain-db/chains/<id>/network/secret_ed25519`). To pin a stable peer id across recreations, generate the key explicitly with `key generate-node-key --file /data/node-key` and add `--node-key-file=/data/node-key` to the validator command. Canonical bootnode operator setup is documented in [`docs/testnet-deployment.md`](docs/testnet-deployment.md).

### Aglais (the Quip test network)

The compose stack joins **Aglais** by default. Aglais is the Quip test network, in the way Sepolia is the Ethereum test network. It started on 2026-09-02 from a fresh genesis and replaced the previous testnet. Identity:

| Field | Value |
|---|---|
| Chain name | `AGLS (Quip Testnet)` |
| Chain id | `quip_testnet` |
| Chain type | `Live` |
| Genesis | `0x59e064bddd49a920d1392693c728c3bf9867f2cf3e0f8fea8c2498b389b0c286` |
| Runtime | spec `117`, transaction version `7` |
| Token | `AGLS` (12 decimals, ss58Format=42) |
| Protocol id | `agls-network` |
| Bootnodes (embedded in spec) | `/dns4/bootnode-{1,2,3}.aglais.quip.network/tcp/30333/p2p/12D3KooW…` |
| Faucet | `https://faucet.aglais.quip.network` |
| Public RPC | `wss://bootnode-{1,2,3}.aglais.quip.network:20049/rpc` |

#### Joining

A fresh `docker compose --profile cpu up -d` boots straight onto Aglais. The spec is committed at `chain-specs/aglais-network.json` with the bootnode addresses embedded, and the validator image is pinned to the Aglais build. No extra bootnode configuration is needed unless you override for a private network.

#### Verifying the spec

The spec ships with a SHA-256 checksum:

```bash
(cd chain-specs && shasum -a 256 -c aglais-network.json.sha256)
# aglais-network.json: OK
```

To verify provenance against the published validator image:

```bash
docker run --rm --entrypoint /usr/local/bin/quip-network-node \
  registry.gitlab.com/quip.network/quip-validator/quip-network-node:v0.3.0-rc1 \
  export-chain-spec --chain quip-testnet --raw > /tmp/from-image.json
shasum -a 256 /tmp/from-image.json chain-specs/aglais-network.json
# Both hashes should match exactly.
```

`--entrypoint` bypasses the image entrypoint, which prints a line to stdout before the JSON.

#### Mirroring procedure

The chain spec is mirrored from `quip-validator`: `node/src/chain_spec.rs::quip_testnet_chain_spec` plus the runtime preset under `runtime/src/genesis_quip_testnet/`. The upstream preset name stays `quip-testnet`. To regenerate after an upstream preset change:

```bash
# Pull the tagged image
docker pull registry.gitlab.com/quip.network/quip-validator/quip-network-node:v0.3.0-rc1

# Re-export and update the checksum sidecar
docker run --rm --entrypoint /usr/local/bin/quip-network-node \
  registry.gitlab.com/quip.network/quip-validator/quip-network-node:v0.3.0-rc1 \
  export-chain-spec --chain quip-testnet --raw > chain-specs/aglais-network.json
(cd chain-specs && shasum -a 256 aglais-network.json > aglais-network.json.sha256)
```

Do not hand-edit `chain-specs/aglais-network.json`. Any change must come from re-exporting after an upstream preset commit.

#### Authorities

Genesis authorities, sudo, and the full set-keys procedure live in [`quip-validator/docs/genesis-quip-testnet.md`](https://gitlab.com/quip.network/quip-validator/-/blob/main/docs/genesis-quip-testnet.md). Operator key handling is documented in [`quip-validator/docs/testnet-keys.md`](https://gitlab.com/quip.network/quip-validator/-/blob/main/docs/testnet-keys.md).

#### Seeding the mining topology

A chain accepts no proof until root registers a topology and marks it mineable. The sudo holder does this once per chain. Operators never run it. Aglais uses the same topology the previous testnet ran, `0xe66d3dfa3c9c6afb15efe29891cf9412498d94692ab5956af3ad98ba3693a02d`: the D-Wave Advantage2 system1 graph, 4577 nodes and 41515 edges, with `allowed_h = [0]`.

That `allowed_h = [0]` is what makes it the h0 topology, and it is why `seed-chain` cannot be run with its defaults here. The built-in `advantage2-system1` preset carries `allowed_h = [-1000, 0, 1000]` and hashes to `0xfb91…7ec4`, a different topology. The matching spec is committed at `config/advantage2-system1-h0.spec.json`.

```bash
docker compose --profile cpu run --rm \
  -v "$PWD/config/advantage2-system1-h0.spec.json:/topology.json:ro" \
  -v /path/to/sudo-mnemonic:/sudo-mnemonic:ro \
  --entrypoint quip-coordinator cpu seed-chain \
    --validator ws://quip-validator:9944 \
    --mnemonic-file /sudo-mnemonic \
    --topology /topology.json \
    --min-solutions 1 \
    --max-energy-milli=-14563316 \
    --min-diversity-milli 0
```

`--max-energy-milli` takes the `=` form because a bare negative value parses as a flag. The three difficulty values are the ones the previous testnet ran, read from its `Difficulties` entry. They are not the `seed-chain` defaults, which are 5, -2500000 and 200. The chain's difficulty controller moves the live threshold from there based on submission rate.

Verify afterwards that `QuantumPow.DefaultTopology` reads back `0xe66d…a02d`. A miner against an unseeded chain logs `feeder: chain has no mining snapshot (no registered/mineable topology); staging nothing` and stages no work.

#### Local development and private networks

For a self-contained dev chain, use `make localdev`. It runs the validator on the image's built-in `--chain=dev` preset, so it always matches the pinned image (see [Local dev chain](#local-dev-chain)).

To join a private network with its own spec, point `QUIP_CHAIN_SPEC` in `.env` at that file. To add bootnodes, append `--bootnodes=<multiaddr>` entries to the validator `command:` via a `docker-compose.override.yml` (compose cannot split one env var into multiple argv tokens, so there is no env knob for this).

### Faucet

The `faucet` profile adds a small HTTP service that signs `Balances.transferKeepAlive` extrinsics from a funded URI-derived account. **Currently dev-only**: the funder is one of `//Alice`, `//Bob`, or `//Alice//stash` and must be funded at genesis on the chain you are running against. Real-keystore support is on the roadmap (see https://gitlab.com/quip.network/faucet). A faucet-only stack (`--profile faucet` with no `cpu` or `cuda` profile) also brings up the dashboard container, which binds ports `80`, `443`, and `20049`.

```bash
# Activate alongside any validator profile (one or both):
docker compose --profile cpu --profile faucet up -d
```

HTTP API (through Caddy):

```bash
# Request funds for an address
curl -fsSL -H 'Content-Type: application/json' \
     -d '{"dest":"<ss58-or-0x-hex>","amount":1000000000000000}' \
     https://<QUIP_HOSTNAME>/api/faucet/request

# Health check
curl -fsSL https://<QUIP_HOSTNAME>/api/faucet/health
```

- `amount` is in plancks (smallest balance unit). Default 1000 UNIT on 12-decimal chains. Optional in the request body.
- Per-destination rate limit defaults to 60s (configurable via `QUIP_FAUCET_RATE_LIMIT_SECONDS`).
- The bot refuses to bind against a non-dev chain unless `QUIP_FAUCET_ALLOW_ANY_CHAIN=1`. Override only when the funder URI is legitimately allocated on the production chain.

### 6. Auto-updates (recommended)

Install an hourly cron job that checks for new images and recreates containers only when digests change:

```bash
./cron.sh --install    # install the hourly cron job
./cron.sh --uninstall  # remove it
./cron.sh              # run a one-off update check
```

`pull_policy: always` on every image ensures the registry is checked each time. If an image hasn't changed, `up -d` is a no-op — no restart, no downtime. Logs are written to `data/update.log`.

## Updating Configuration

After editing `data/config.toml`, restart the node to pick up changes:

```bash
docker compose restart cpu   # or cuda
```

The config file is bind-mounted, so restarting re-reads it from disk. Use `--force-recreate` only if you change `.env` or `docker-compose.yml` (environment variables are baked into the container at creation time):

```bash
docker compose --profile cpu up -d --force-recreate
```

## Logs

Every service except the collector writes to one merged file, `data/logs/quip-node.log`. Each line carries the container name, so one `tail` shows the whole stack:

    make logs

This tails the merged file with a 200-line window. If the file does not exist yet (first boot), it falls back to `docker compose logs -f --tail=50` across the whole project. The collector is the exception — it stays on Docker's json-file driver so its own startup errors remain readable when the merged file is broken or missing. Read collector errors with `docker compose logs dashboard`. The collector output does not appear in the merged file.

The collector rotates the file at 10 MB and keeps 5 generations, the same as the v0.1 miner did. `docker compose logs` also still works, served from Docker's local cache rather than from the file.

**The merged file is best-effort, by design.** Every service reaches the collector over UDP so that a stalled or restarting collector never blocks a producer's startup (see `deploy/syslog-ng/syslog-ng.conf` in the dashboard repository). The tradeoff is dropped lines under a burst: a single container emitting a 20000-line burst can lose around 15 percent of them at the kernel's default receive buffer. `docker compose logs <service>` (or `make logs` equivalents per service) reads from Docker's own cache instead of the network and does not drop lines; treat the merged file as a convenience view and the per-service logs as the record of truth when every line matters. The collector raises its UDP receive buffer (`so-rcvbuf`) to narrow the loss window, but the kernel still caps it at `net.core.rmem_max`; raise that on the host if you need it higher, for example `sysctl -w net.core.rmem_max=8388608`.

**The merged file is operational, not an audit log.** Any local process on the host can send a UDP datagram to the collector's port and have it appear as a line in `data/logs/quip-node.log`, tagged with whatever program name it chooses. Do not rely on this file to prove what a service did or did not log.

A single log line over 16 KB (for example a substrate panic or a RocksDB error dump) arrives in the merged file as several separately timestamped records instead of one. This is a limit of Docker's log copier, not of syslog-ng, and cannot be changed from this side — recognize a multi-part stack trace by matching timestamps.

**Known limitation:** `make logs` always tails `data/logs/quip-node.log`, the testnet stack's merged file. The localdev stack writes its own merged log to `data/logs-localdev/quip-node.log`, so `make logs` never shows localdev's merged output — read it directly, or use `docker compose logs -f <service>` against the localdev project.

**If the dashboard container fails to start**, its fixed host port may already be in use — a leftover container, a host syslog daemon, or another stack. Check with `ss -lunp | grep 5514` and free the port, or set `QUIP_LOG_PORT` in `.env` to move the collector off 5514. No other service depends on the dashboard container, so this failure does not block the validator, miner, or faucet.

If you have v0.1 logs, move any existing `data/logs/quip-node.log*` files into `data/logs/archive-v0.1/` before first start. The rotation would otherwise interleave stale v0.1 miner output with new merged output. Use these commands:

```bash
mkdir -p data/logs/archive-v0.1
mv data/logs/quip-node.log* data/logs/archive-v0.1/ 2>/dev/null || true
```

Run this only before first start, or stop the stack first. Moving the live file out from under a running collector unlinks it while syslog-ng still holds it open; the supervisor detects this and restarts syslog-ng automatically within one `QUIP_LOG_CHECK_INTERVAL`, but you can avoid even that gap by stopping the stack first or running `docker restart quip-dashboard` immediately afterward.

## Maintenance

| Task | Command |
|------|---------|
| View merged stack log | `make logs` |
| View miner logs | `docker compose logs -f cpu` (or `cuda`) |
| View validator logs | `docker compose logs -f quip-validator` |
| View faucet logs | `docker compose logs -f quip-faucet` |
| View dashboard logs | `docker compose logs -f dashboard` |
| View Caddy / TLS logs | `docker compose logs -f dashboard` |
| View auto-update logs | `tail -f data/update.log` |
| Restart after config change | `docker compose restart cpu` |
| Restart after .env change | `docker compose --profile cpu up -d --force-recreate` |
| Force pull and redeploy | `docker compose pull cpu && docker compose up -d cpu` |
| Stop everything | `docker compose --profile cpu --profile faucet down` |

Changing `QUIP_LOG_MAX_BYTES` or `QUIP_LOG_KEEP` requires a container recreate, the same as changes to the cache size vars. Use `docker compose --profile cpu up -d --force-recreate` (or `cuda`).

## Files

| File | Purpose |
|------|---------|
| `docker-compose.yml` | Validator + miner + faucet + dashboard services |
| `config/config.example.toml` | Canonical v0.3 config example, documented key by key (reference only, not mounted) |
| `data/config.toml` | Active node configuration (copied from a template) |
| `data/config.cpu.toml` | CPU mode template (base for QPU/D-Wave; uncomment `[qpu]` + `[dwave]`) |
| `data/config.cuda.toml` | CUDA GPU mode template |
| `chain-specs/aglais-network.json` | Aglais (Quip test network) chain spec (committed; mirrored from quip-validator) |
| `chain-specs/aglais-network.json.sha256` | SHA-256 checksum for the Aglais spec |
| `data/aglais-chain-db/` | Validator base path (keystore, db, libp2p key; gitignored) |
| `docs/testnet-deployment.md` | Operator host setup for canonical testnet bootnode validators |
| `scripts/sysctl-tune.sh` | Host kernel tuning (BBR + fq + no slow-start-after-idle) |
| `scripts/validator-healthcheck.sh` | Validator sync gate, mounted into the validator container as its healthcheck |
| `.env` | Compose interpolation source: QUIP_HOSTNAME, CERT_EMAIL, DWAVE_API_TOKEN, DWAVE_API_SOLVER, tags + knobs (not checked in) |
| `env.example` | Template for `.env` |
| `config/quip-miner.toml` | Miner first-run config template (Aglais faucet_url), mounted over the image's `/app/config.toml` |
| `config/advantage2-system1-h0.spec.json` | Topology spec the network mines against (`0xe66d…a02d`). Input to `seed-chain`, not read at runtime |
| `config/localdev.{cpu,cuda}.toml` | Localdev miner configs; `make localdev` copies the profile's variant to `data/config.toml` |
| `dashboard-data/` | Dashboard index database (`dashboard.db`), `/files` data, and collector state (bind mount, gitignored) |
| `quip-caddy-data` | Docker named volume for Caddy's certs + state, mounted into the dashboard container |
| `quip-caddy-config` | Docker named volume for Caddy's autosaved config, mounted into the dashboard container |
