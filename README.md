# Quip Network Node - Docker Deployment

Quick-start Docker Compose deployment for Quip Network nodes. Supports CPU, CUDA (GPU), and QPU (D-Wave) mining modes via compose profiles. Each node profile also brings up a Caddy reverse proxy, the telemetry dashboard, and a bundled Postgres backend — so operators get a single-URL monitoring UI with automatic TLS out of the box.

## Architecture

```
Internet
  ├─ 443/tcp, 80/tcp → quip-caddy ─┬─ /api/v1/*  → quip-node:80  (REST)
  │                                └─ /*         → quip-dashboard:3001
  └─ 20049/udp+tcp  → quip-node (QUIC peer-to-peer)
```

Caddy handles HTTP(S) as a single front door: the dashboard SPA at `/` and
node REST under `/api/v1/*`. TLS is automatic — Caddy's ACME client
provisions and renews Let's Encrypt certs whenever `QUIP_HOSTNAME` is a
real DNS name. QUIC peer-to-peer stays on the raw node at port 20049.

## Setup

### 1. Choose a mode and copy the config template

```bash
# CPU mining
cp data/config.cpu.toml data/config.toml

# CUDA GPU mining (requires NVIDIA GPU + drivers)
cp data/config.cuda.toml data/config.toml

# QPU mining (D-Wave)
cp data/config.qpu.toml data/config.toml
```

### 2. Configure the node

Edit `data/config.toml`:
- Set `secret` to a unique value for your node's identity
- Set `public_host` to your server's public hostname (DNS name, not IP)
- Adjust `node_name` and `peer` list as needed
- For QPU: solver and daily budget are pre-configured for Advantage2

### 3. Configure credentials

```bash
cp env.example .env
printf 'PUID=%s\nPGID=%s\n' "$(id -u)" "$(id -g)" >> .env
# Edit .env:
#   QUIP_HOSTNAME — 'localhost' (default) serves HTTP only. Set to a real DNS name
#                   to enable Caddy's automatic Let's Encrypt TLS.
#   CERT_EMAIL    — email registered with Let's Encrypt; required when QUIP_HOSTNAME
#                   is a real DNS name.
#   DWAVE_API_KEY — D-Wave API token (QPU only)
#   POSTGRES_PASSWORD — optional; defaults to 'quip'. Postgres is not exposed to the
#                       host, so the default is safe for local use.
```

The `printf` line seeds `.env` with your host's uid/gid so files under `./data/` stay editable without `sudo`. Since quip-protocol v0.1.7 the node runs as a non-root `quip` user and chowns `/data` to match `PUID`/`PGID` on start (default 1000).

### 4. (Recommended) Tune the host kernel

Apply BBR + fair-queueing + no slow-start-after-idle on the host — improves throughput for long-lived TCP and is required for BBR's packet pacing:

```bash
sudo ./scripts/sysctl-tune.sh
```

Idempotent. Writes `/etc/sysctl.d/99-quip.conf` and runs `sysctl --system`. Needs kernel ≥ 4.9 (every supported Ubuntu LTS qualifies).

### 5. Start

```bash
# CPU node
docker compose --profile cpu up -d

# CUDA GPU node
docker compose --profile cuda up -d

# QPU node
docker compose --profile qpu up -d
```

Each command starts four containers: the chosen node (`quip-cpu`/`quip-cuda`/`quip-qpu`), the telemetry dashboard (`quip-dashboard`), a Postgres database (`quip-postgres`), and Caddy (`quip-caddy`).

**Monitor your node at [http://localhost/](http://localhost/)** — or `https://<QUIP_HOSTNAME>/` when running on a remote machine with TLS.

To run a node on its own without the dashboard, Postgres, and Caddy, use the `-nodash` profile variant:

```bash
docker compose --profile cpu-nodash up -d   # or cuda-nodash, qpu-nodash
```

`cron.sh` detects which variant is running and preserves your choice on auto-update.

### TLS

With `QUIP_HOSTNAME=localhost` (the default) Caddy serves HTTP on `:80` and self-signs `:443` with its internal CA — browsers will warn on HTTPS, so stick to `http://localhost/` for local use.

When `QUIP_HOSTNAME` is a real DNS name that resolves to your host, Caddy provisions a Let's Encrypt cert via HTTP-01 on `:80`, serves HTTPS on `:443`, and redirects HTTP to HTTPS. Set `CERT_EMAIL` in `.env` so Caddy can register its ACME account. Port 80 must be reachable from the internet during provisioning and every renewal.

Certs persist in the `quip-caddy-data` named volume across container recreations.

**QUIC transport TLS** (for node-to-node peer traffic on 20049) is handled by the node itself via the TOFU (trust-on-first-use) model backed by `trust.db`. Sharing Caddy's Let's Encrypt cert into QUIC is not yet wired up here — see [TLS.md](https://gitlab.com/quip.network/quip-protocol/-/blob/main/docker/TLS.md) in quip-protocol for manual configuration.

### Dashboard

The dashboard indexer polls the local node over the compose network (`http://quip-node:80`). The config templates ship with `rest_insecure_port = 80` enabled inside the node container, so this works out of the box. The node's REST is **not** exposed to the host directly — all external traffic goes through Caddy.

To point the dashboard at a public full node instead of the local one, set `QUIP_NODE_URL` in `.env`:

```bash
QUIP_NODE_URL=https://qpu-1.nodes.quip.network
```

Telemetry persists in the `quip-pgdata` named volume, so it survives container recreations.

### 6. Auto-updates (recommended)

Install an hourly cron job that checks for new images and recreates containers only when digests change:

```bash
./cron.sh --install    # install the hourly cron job
./cron.sh --uninstall  # remove it
./cron.sh              # run a one-off update check
```

`pull_policy: always` on node/dashboard/caddy ensures the registry is checked each time. If an image hasn't changed, `up -d` is a no-op — no restart, no downtime. Logs are written to `data/update.log`.

## Updating Configuration

After editing `data/config.toml`, restart the node to pick up changes:

```bash
docker compose restart qpu
```

The config file is bind-mounted, so restarting re-reads it from disk. Use `--force-recreate` only if you change `.env` or `docker-compose.yml` (environment variables are baked into the container at creation time):

```bash
docker compose --profile qpu up -d --force-recreate
```

## Maintenance

| Task | Command |
|------|---------|
| View node logs | `docker compose logs -f cpu` (or `cuda`, `qpu`) |
| View dashboard logs | `docker compose logs -f dashboard` |
| View Caddy / TLS logs | `docker compose logs -f caddy` |
| View auto-update logs | `tail -f data/update.log` |
| Restart after config change | `docker compose restart cpu` |
| Restart after .env change | `docker compose --profile cpu up -d --force-recreate` |
| Force pull and redeploy | `docker compose pull cpu && docker compose up -d cpu` |
| Stop everything | `docker compose --profile cpu down` |

## Files

| File | Purpose |
|------|---------|
| `docker-compose.yml` | Node + dashboard + postgres + caddy services (cpu/cuda/qpu profiles) |
| `caddy/Caddyfile` | Reverse-proxy + auto-TLS config for the Caddy front door |
| `data/config.toml` | Active node configuration (copied from a template) |
| `data/config.cpu.toml` | CPU mode template |
| `data/config.cuda.toml` | CUDA GPU mode template |
| `data/config.qpu.toml` | QPU (D-Wave) mode template |
| `scripts/sysctl-tune.sh` | Host kernel tuning (BBR + fq + no slow-start-after-idle) |
| `.env` | QUIP_HOSTNAME, CERT_EMAIL, DWAVE_API_KEY, optional POSTGRES_PASSWORD (not checked in) |
| `env.example` | Template for `.env` |
| `dashboard-data/` | Dashboard auxiliary state (bind mount, gitignored) |
| `quip-pgdata` | Docker named volume for Postgres data |
| `quip-caddy-data` | Docker named volume for Caddy's certs + state |
| `quip-caddy-config` | Docker named volume for Caddy's autosaved config |
