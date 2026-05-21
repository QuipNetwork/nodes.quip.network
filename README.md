# Quip Network Node - Docker Deployment

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
  └─ 30333/tcp+udp → quip-validator (substrate libp2p, validator profiles only)
```

Caddy is the single front door — there are no other host port publishes. The miner runs purely as an outbound substrate RPC client (no inbound QUIC, no inbound REST) and is reachable only over the compose network. Substrate RPC is at `/rpc`, faucet at `/api/faucet/*`, miner telemetry at `/api/v1/*`, dashboard SPA at `/`. All four are served on **both** `:443` and `:20049` in TLS mode, or on `:20049` HTTP-only in dev mode. `:80` is used for ACME HTTP-01 challenges and auto-redirects to `:443`.

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
- Set `secret` to a unique value for your node's identity
- Adjust `node_name` for telemetry display
- Override the `validators` list if you're co-locating a validator (use `ws://quip-validator:9944`) or connecting to a non-default network
- For QPU (D-Wave): uncomment the `[qpu]` and `[dwave]` sections at the bottom of `config.cpu.toml`. Solver and budget are pre-set for Advantage2.

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
| `cpu` | miner (CPU), dashboard, postgres, Caddy | Default. Uncomment `[qpu]` + `[dwave]` in `config.toml` for D-Wave. |
| `cuda` | miner (CUDA), dashboard, postgres, Caddy | Requires NVIDIA GPU + Docker GPU runtime. |
| `validator-cpu` | `cpu` services + `quip-validator` | Requires production `QUIP_HOSTNAME` (TLS-only `/rpc`). |
| `validator-cuda` | `cuda` services + `quip-validator` | Requires production `QUIP_HOSTNAME`. |

The `faucet` profile layers additively on top of any validator profile:

```bash
# Miner only
docker compose --profile cpu up -d

# CUDA miner
docker compose --profile cuda up -d

# Validator + colocated CPU miner
docker compose --profile validator-cpu up -d

# Validator + faucet
docker compose --profile validator-cpu --profile faucet up -d
```

**Monitor your node at [http://localhost:20049/](http://localhost:20049/)** — or `https://<QUIP_HOSTNAME>/` (and `:20049`) when running on a remote machine with TLS.

`cron.sh` detects which profiles are running (based on which `quip-*` containers exist) and preserves them on auto-update.

### TLS

With the default `QUIP_HOSTNAME=:20049`, Caddy serves HTTP on port 20049 with no TLS — good for local dev with the `cpu` / `cuda` profiles. Access the dashboard at `http://localhost:20049/`.

For production, set `QUIP_HOSTNAME` to the comma-separated form (`example.com, example.com:20049`) and `CERT_EMAIL` to a valid address. Caddy provisions a Let's Encrypt cert via HTTP-01 on `:80`, serves HTTPS on `:443` and `:20049`, and redirects HTTP to HTTPS. Port 80 must be reachable from the internet during provisioning and every renewal.

The default ACME issuer is **Let's Encrypt**, with **ZeroSSL** as an automatic fallback (built-in to Caddy 2.6+). To pin ZeroSSL as the primary issuer — useful if you want longer cert validity or have hit LE rate limits — uncomment the `cert_issuer zerossl` line in `caddy/Caddyfile` and optionally set `ZEROSSL_API_KEY` in `.env` for pre-provisioned EAB credentials. For DNS-01 challenges or other CAs, edit `caddy/Caddyfile` — see the [Caddy docs](https://caddyserver.com/docs/automatic-https).

Certs persist in the `quip-caddy-data` named volume across container recreations.

### Dashboard

The dashboard indexer polls the local node over the compose network (`http://quip-node:80`). The config templates ship with `rest_insecure_port = 80` enabled inside the node container, so this works out of the box. The node's REST is **not** exposed to the host directly — all external traffic goes through Caddy.

To point the dashboard at a public full node instead of the local one, set `QUIP_NODE_URL` in `.env`:

```bash
QUIP_NODE_URL=https://cpu-1.nodes.quip.network
```

Telemetry persists in the `quip-pgdata` named volume, so it survives container recreations.

### Validator setup

Running a validator means joining substrate consensus. Prerequisites:

- **Production `QUIP_HOSTNAME`** — validator profiles serve substrate RPC at `wss://<host>/rpc` and `wss://<host>:20049/rpc`. Both must be TLS, so a real DNS name is required.
- **Inbound 30333/tcp and 30333/udp** reachable from the public internet for libp2p peering, in addition to 80/443/20049.
- **Docker Compose v2.20+** for the `depends_on.required: false` flag used to make the validator a soft dependency of the miner. On older versions, remove that line from `docker-compose.yml` (miner will retry RPC connection on startup either way).

The validator boots against `chain-specs/quip-testnet.json` by default (see [Quip Testnet](#quip-testnet) below). For local development against the `quip-local` preset, set `QUIP_CHAIN_SPEC=./data/chain-spec.json` in `.env`.

```bash
# 1. Start the stack — chain spec is already in place
docker compose --profile validator-cpu up -d

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

A fresh `docker compose --profile validator-cpu up -d` boots straight onto Quip Testnet — the spec is committed at `chain-specs/quip-testnet.json`, the v0.2-preview validator image is pinned by default, and the bootnode addresses are embedded in the spec (no `SUBSTRATE_BOOTNODES` env var needed unless you're overriding for a private network).

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
docker compose --profile validator-cpu --profile faucet up -d
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
| Stop everything | `docker compose --profile validator-cpu --profile faucet down` |

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
