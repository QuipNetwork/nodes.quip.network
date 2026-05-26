# Quip Network Node - Docker Deployment

> **Upgrading from v0.1?** See [Upgrading from v0.1](#upgrading-from-v01) below — the miner config schema changed, container/image names changed, and the substrate validator now owns p2p. There's a one-time `make updateconfig` step plus a few docker compose differences worth scanning before you bring things up.

Quick-start Docker Compose deployment for Quip Network nodes. Supports CPU and CUDA (GPU) mining, with an optional Substrate-based validator and faucet sidecar. Each profile brings up a Caddy reverse proxy, the telemetry dashboard, and a bundled Postgres backend — so operators get a single-URL monitoring UI with automatic TLS out of the box.

## Architecture

```
Internet
  ├─ 80/tcp  → quip-caddy (TLS mode: auto-redirect to :443)
  ├─ 443/tcp → quip-caddy ─┬─ /rpc/*        → quip-validator:9944  (substrate RPC, validator profiles)
  │                        ├─ /api/faucet/* → quip-faucet:8087     (faucet profile)
  │                        ├─ /api/v1/*     → quip-node:80         (miner telemetry)
  │                        └─ /*            → quip-dashboard:3001  (dashboard SPA)
  ├─ 20049/tcp → quip-caddy (same routes as :443; the canonical Quip API port)
  └─ 30333/tcp+udp → quip-validator (substrate libp2p, bundled into every profile)
```

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

## Upgrading from v0.1

The v0.2 stack replaces the v0.1 P2P mesh with a substrate validator + RPC-client miner. That changes the binary (`quip-node` → `quip-miner`), the image names (`quip-network-node-{cpu,cuda}` → `quip-miner-{cpu,cuda}`), the compose services (`qpu` collapsed into `cpu` + a `[qpu]` config section; new `quip-validator` and `quip-faucet` services), and the config schema (`[global]` → `[miner]`, P2P/TLS keys removed, `validators` + `signer_key` required). Full schema diff in [CHANGELOG.md](CHANGELOG.md).

### 1. Stop and remove the v0.1 containers

The v0.1 container names (`quip-cpu`, `quip-cuda`, `quip-qpu`, `quip-dashboard`, `quip-postgres`, `quip-caddy`) still exist in v0.2 except for `quip-qpu`, so a plain `docker compose down` from the new tree won't necessarily reach them if you've already pulled v0.2. Stop and remove them explicitly first — `|| true` makes this safe to copy/paste even if some containers don't exist on your host:

```bash
docker stop quip-cpu quip-cuda quip-qpu quip-dashboard quip-postgres quip-caddy 2>/dev/null || true
docker rm   quip-cpu quip-cuda quip-qpu quip-dashboard quip-postgres quip-caddy 2>/dev/null || true
```

Your data is in bind mounts (`./data/`, `./dashboard-data/`) and named volumes (`quip-pgdata`, `quip-caddy-data`, `quip-caddy-config`), so removing containers is non-destructive.

### 2. Pull the v0.2 repo

```bash
git pull origin v0.2
```

Review `docker-compose.yml` and `env.example` against your local `.env`:

- **New env vars** you'll want to set before the first start: `QUIP_VALIDATOR_TAG`, `QUIP_FAUCET_TAG`, `QUIP_MINER_CPUSET`, `VALIDATOR_NAME`, `SUBSTRATE_BOOTNODES`, `CERT_EMAIL`.
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
- writes a fresh `data/config.toml` in v0.2 shape, carrying over `node_name`, `public_host`, `public_port`, `rest_host`, `rest_port`, `log_level`, `node_log` and preserving backend tables (`[cpu]`, `[gpu]`, `[cuda.N]`, `[qpu]`, `[dwave]`, …) verbatim
- defaults `validators = ["ws://quip-validator:9944"]` — the local bundled validator. It peers with the testnet bootnodes via libp2p on `:30333`, so this entry is correct out of the box and shouldn't be edited
- defaults `signer_key = "/data/keystore.json"` — the entrypoint auto-generates the hybrid keystore on first start
- warns loudly about dropped `[global].port` / `[global].listen` (semantics flipped from QUIC peer to telemetry REST — the v0.2 loader would silently alias these, but that risks exposing the REST API on what used to be the peer port)

Idempotent: re-running on an already-converted dir exits with "nothing to do".

### 4. Decide on the ACME challenge type

Caddy auto-provisions a Let's Encrypt cert for `QUIP_HOSTNAME` in production mode. Two ways it can prove control of the DNS name:

| Challenge | What you need | When to pick |
|---|---|---|
| **HTTP-01** (default) | Port **80** reachable from the public internet | Simplest. Works out of the box with `caddy:2-alpine`. Required if you can't or won't share DNS API credentials with the host. |
| **DNS-01** | A custom Caddy image with your DNS provider plugin compiled in (`caddy-dns/cloudflare`, `caddy-dns/route53`, `caddy-dns/digitalocean`, …) **and** DNS-API credentials in `.env` | Required if your host can't bind `:80` (firewalled, port already taken, behind a NAT without port-forward). Also supports wildcard certs. |

For HTTP-01, no extra config — just make sure `:80` is open and `CERT_EMAIL` is set in `.env`. For DNS-01, build a Caddy image with your provider's plugin (see [Caddy's DNS challenge docs](https://caddyserver.com/docs/automatic-https#dns-challenge)), swap the `image:` line for the `caddy` service in `docker-compose.yml`, and add the appropriate `tls { dns <provider> }` block in `caddy/Caddyfile`. The plumbing is out of scope for this repo because the credential surface is provider-specific.

### 5. Bring the v0.2 stack up

```bash
# CPU miner + bundled local validator + dashboard + Caddy
docker compose --profile cpu up -d

# CUDA miner + bundled local validator + dashboard + Caddy
docker compose --profile cuda up -d

# Layer in the faucet (dev only)
docker compose --profile cpu --profile faucet up -d
```

Both `cpu` and `cuda` profiles bundle a local substrate validator by default — there's no separate validator profile anymore. The first start pulls the v0.2 images (`quip-miner-{cpu,cuda}`, `quip-network-node`, `quip-faucet`, etc.), auto-generates `data/keystore.json` for the miner, and the miner entrypoint then calls out to the canonical Quip Testnet faucet (`https://faucet.testnet.quip.network`, wired by default in `docker-compose.yml`) to register the new account on-chain and fund it for the first proof submission. Set `QUIP_FAUCET_URL=` (empty) in `.env` to opt out if you've pre-funded the account yourself.

Check it came up cleanly:

```bash
docker compose --profile cpu ps
docker compose logs -f cpu              # miner
docker compose logs -f quip-validator   # validator
```

Then visit the dashboard at `http://localhost:20049/` (dev) or `https://<your-hostname>/` (production).

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
- `DWAVE_API_KEY` — required only for QPU / D-Wave mining.
- `POSTGRES_PASSWORD` — optional; defaults to `quip`. Postgres isn't published to the host, so the default is safe for local use.
- `QUIP_VALIDATOR_TAG`, `VALIDATOR_NAME`, `SUBSTRATE_BOOTNODES` — see `env.example` for the validator and faucet sections.

The `printf` line seeds `.env` with your host's uid/gid so files under `./data/` stay editable without `sudo`. Since quip-protocol v0.1.7 the node runs as a non-root `quip` user and chowns `/data` to match `PUID`/`PGID` on start (default 1000).

### 4. (Recommended) Tune the host kernel

Apply BBR + fair-queueing + no slow-start-after-idle on the host — improves throughput for long-lived TCP and is required for BBR's packet pacing:

```bash
sudo ./scripts/sysctl-tune.sh
```

Idempotent. Writes `/etc/sysctl.d/99-quip.conf` and runs `sysctl --system`. Needs kernel ≥ 4.9 (every supported Ubuntu LTS qualifies).

### 5. Start

Four primary profiles are available:

| Profile | Includes | Notes |
|---|---|---|
| `cpu` | miner (CPU), local validator, dashboard, postgres, Caddy | Default. Uncomment `[qpu]` + `[dwave]` in `config.toml` for D-Wave. |
| `cuda` | miner (CUDA), local validator, dashboard, postgres, Caddy | Requires NVIDIA GPU + Docker GPU runtime. |

Every node bundles its own substrate validator — there's no separate validator-only or miner-only profile. The `faucet` profile layers additively for dev chains:

```bash
# CPU miner + local validator
docker compose --profile cpu up -d

# CUDA miner + local validator
docker compose --profile cuda up -d

# Add the faucet (dev only)
docker compose --profile cpu --profile faucet up -d
```

**Monitor your node at [http://localhost:20049/](http://localhost:20049/)** — or `https://<QUIP_HOSTNAME>/` (and `:20049`) when running on a remote machine with TLS.

`cron.sh` detects which profiles are running (based on which `quip-*` containers exist) and preserves them on auto-update.

### TLS

With the default `QUIP_HOSTNAME=:20049`, Caddy serves HTTP on port 20049 with no TLS — good for local dev with the `cpu` / `cuda` profiles. Access the dashboard at `http://localhost:20049/`.

For production, set `QUIP_HOSTNAME` to the comma-separated form (`example.com, example.com:20049`) and `CERT_EMAIL` to a valid address. Caddy provisions a Let's Encrypt cert via HTTP-01 on `:80`, serves HTTPS on `:443` and `:20049`, and redirects HTTP to HTTPS. Port 80 must be reachable from the internet during provisioning and every renewal — if it isn't (firewalled host, port already taken, NAT without port-forward), see the [DNS-01 alternative](#4-decide-on-the-acme-challenge-type) in the upgrade flow.

The default ACME issuer is **Let's Encrypt**, with **ZeroSSL** as an automatic fallback (built-in to Caddy 2.6+). To pin ZeroSSL as the primary issuer — useful if you want longer cert validity or have hit LE rate limits — uncomment the `cert_issuer zerossl` line in `caddy/Caddyfile` and optionally set `ZEROSSL_API_KEY` in `.env` for pre-provisioned EAB credentials.

Certs persist in the `quip-caddy-data` named volume across container recreations.

### Dashboard

The dashboard indexer polls the local validator over the compose network (`ws://quip-validator:9944`) for both chain state and the miner REST surface that Caddy fronts on the same host. The node's RPC is **not** exposed to the host directly — all external traffic goes through Caddy.

For miner-only nodes (no colocated validator), point the indexer at a public full node via `QUIP_VALIDATOR_RPC_URLS` in `.env`. The value is comma-separated; the indexer rotates through the list on failure:

```bash
QUIP_VALIDATOR_RPC_URLS=wss://cpu-1.nodes.quip.network/rpc
```

Telemetry persists in the `quip-pgdata` named volume, so it survives container recreations.

### Validator setup

Every node bundles a local substrate validator, so just starting the `cpu` or `cuda` profile makes you a validator. What changes by deployment:

- **Inbound `:20049`** (Caddy) — required for the dashboard/RPC surface to be reachable from the public internet.
- **Inbound `:30333/tcp+udp`** (libp2p) — same priority as `:20049`. Lets other validators dial yours so you're a participating peer instead of a leaf. Mining works without it (outbound to bootnodes is enough), but your peer count stays in the single digits.
- **TLS for the public RPC** is best-effort and Caddy-driven. Set `QUIP_HOSTNAME` to your real DNS name + `CERT_EMAIL`, open port 80 (HTTP-01) or wire DNS-01, and Caddy serves `wss://<host>/rpc` and `wss://<host>:20049/rpc`. Without those, the validator still runs locally and is reachable on the compose network (`ws://quip-validator:9944`) — only the public WSS endpoint is gated on the cert.
- **Docker Compose v2.20+** for the `depends_on.required: false` flag used to make the validator a soft dependency of the miner. On older versions, remove that line from `docker-compose.yml` (miner will retry RPC connection on startup either way).

After bringing the stack up, verify `:20049` and `:30333` are reachable from the public internet via [`check.quip.network`](https://check.quip.network) — see the [architecture section](#architecture) for the exact curl invocations.

The validator boots against `chain-specs/quip-testnet.json` by default (see [Quip Testnet](#quip-testnet) below). For local development against the `quip-local` preset, set `QUIP_CHAIN_SPEC=./data/chain-spec.json` in `.env`.

```bash
# 1. Start the stack — chain spec is already in place
docker compose --profile cpu up -d

# 3. Rotate session keys — substrate generates and stores them under the
#    keystore in ./data/validator-data/chains/<id>/keystore/. The returned
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
- The validator's libp2p node key is auto-generated at first start (under `data/validator-data/chains/<id>/network/secret_ed25519`). To pin a stable peer id across recreations, generate the key explicitly with `key generate-node-key --file /data/node-key` and add `--node-key-file=/data/node-key` to the validator command. Canonical bootnode operator setup is documented in [`docs/testnet-deployment.md`](docs/testnet-deployment.md).

### Quip Testnet

The compose stack joins the canonical **Quip Testnet** by default. Identity:

| Field | Value |
|---|---|
| Chain name | `Quip Testnet` |
| Chain id | `quip_testnet` |
| Chain type | `Live` |
| Token | `tQUIP` (12 decimals, ss58Format=42) |
| Protocol id | `quip-testnet` |
| Bootnodes (embedded in spec) | `/dns4/bootnode-{1,2,3}.testnet.quip.network/tcp/30333/p2p/12D3KooW…` |

#### Joining

A fresh `docker compose --profile cpu up -d` boots straight onto Quip Testnet — the spec is committed at `chain-specs/quip-testnet.json`, the v0.2-preview validator image is pinned by default, and the bootnode addresses are embedded in the spec (no `SUBSTRATE_BOOTNODES` env var needed unless you're overriding for a private network).

#### Verifying the spec

The spec ships with a SHA-256 checksum:

```bash
(cd chain-specs && shasum -a 256 -c quip-testnet.json.sha256)
# quip-testnet.json: OK
```

To verify provenance against the published validator image:

```bash
docker run --rm \
  registry.gitlab.com/quip.network/quip-protocol-rs/quip-network-node:v0.2-preview \
  export-chain-spec --chain quip-testnet --raw > /tmp/from-image.json
shasum -a 256 /tmp/from-image.json chain-specs/quip-testnet.json
# Both hashes should match exactly.
```

#### Mirroring procedure

The chain spec is mirrored from `quip-protocol-rs` — specifically `runtime/src/genesis_quip_testnet/` plus the inline tx-account hex in `genesis_config_presets.rs::quip_testnet_config_genesis`. To regenerate after an upstream preset change:

```bash
# Pull the new preview image (after upstream pushes the new sha-XXXXXXXX tag)
docker pull registry.gitlab.com/quip.network/quip-protocol-rs/quip-network-node:v0.2-preview

# Re-export and update the checksum sidecar
docker run --rm registry.gitlab.com/quip.network/quip-protocol-rs/quip-network-node:v0.2-preview \
  export-chain-spec --chain quip-testnet --raw > chain-specs/quip-testnet.json
(cd chain-specs && shasum -a 256 quip-testnet.json > quip-testnet.json.sha256)
```

Do not hand-edit `chain-specs/quip-testnet.json`. Any change must come from re-exporting after an upstream preset commit.

#### Authorities

Genesis authorities, sudo, and the full set-keys procedure live in [`quip-protocol-rs/docs/genesis-quip-testnet.md`](https://gitlab.com/quip.network/quip-protocol-rs/-/blob/v0.2/docs/genesis-quip-testnet.md). Operator key handling is documented in [`quip-protocol-rs/docs/testnet-keys.md`](https://gitlab.com/quip.network/quip-protocol-rs/-/blob/v0.2/docs/testnet-keys.md).

#### Switching to local development

Set `QUIP_CHAIN_SPEC` in `.env` to flip the validator to the `quip-local` preset that ships at `data/chain-spec.json`:

```bash
QUIP_CHAIN_SPEC=./data/chain-spec.json
```

The local preset has //Alice/Bob/etc. pre-funded so the faucet works against it without `QUIP_FAUCET_ALLOW_ANY_CHAIN=1`. Bootnodes are empty in the local spec; provide them via `SUBSTRATE_BOOTNODES` if you're joining a private network.

### Faucet

The `faucet` profile adds a small HTTP service that signs `Balances.transferKeepAlive` extrinsics from a funded URI-derived account. **Currently dev-only**: the funder is one of `//Alice`, `//Bob`, or `//Alice//stash` and must be funded at genesis on the chain you're running against. Real-keystore support is on the roadmap (see https://gitlab.com/quip.network/faucet).

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

## Maintenance

| Task | Command |
|------|---------|
| View miner logs | `docker compose logs -f cpu` (or `cuda`) |
| View validator logs | `docker compose logs -f quip-validator` |
| View faucet logs | `docker compose logs -f quip-faucet` |
| View dashboard logs | `docker compose logs -f dashboard` |
| View Caddy / TLS logs | `docker compose logs -f caddy` |
| View auto-update logs | `tail -f data/update.log` |
| Restart after config change | `docker compose restart cpu` |
| Restart after .env change | `docker compose --profile cpu up -d --force-recreate` |
| Force pull and redeploy | `docker compose pull cpu && docker compose up -d cpu` |
| Stop everything | `docker compose --profile cpu --profile faucet down` |

## Files

| File | Purpose |
|------|---------|
| `docker-compose.yml` | Node + validator + faucet + dashboard + postgres + caddy services |
| `caddy/Caddyfile` | Reverse-proxy + auto-TLS config for the Caddy front door |
| `data/config.toml` | Active node configuration (copied from a template) |
| `data/config.cpu.toml` | CPU mode template (base for QPU/D-Wave; uncomment `[qpu]` + `[dwave]`) |
| `data/config.cuda.toml` | CUDA GPU mode template |
| `chain-specs/quip-testnet.json` | Canonical Quip Testnet chain spec (committed; mirrored from quip-protocol-rs) |
| `chain-specs/quip-testnet.json.sha256` | SHA-256 checksum for the testnet spec |
| `data/chain-spec.json` | Local-development chain spec (`quip-local`; opt-in via `QUIP_CHAIN_SPEC`) |
| `data/validator-data/` | Validator base path (keystore, db, libp2p key; gitignored) |
| `docs/testnet-deployment.md` | Operator host setup for canonical testnet bootnode validators |
| `scripts/sysctl-tune.sh` | Host kernel tuning (BBR + fq + no slow-start-after-idle) |
| `.env` | QUIP_HOSTNAME, CERT_EMAIL, DWAVE_API_KEY, validator + faucet env (not checked in) |
| `env.example` | Template for `.env` |
| `dashboard-data/` | Dashboard auxiliary state (bind mount, gitignored) |
| `quip-pgdata` | Docker named volume for Postgres data |
| `quip-caddy-data` | Docker named volume for Caddy's certs + state |
| `quip-caddy-config` | Docker named volume for Caddy's autosaved config |
