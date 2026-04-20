# Quip Network Node - Docker Deployment

Quick-start Docker Compose deployment for Quip Network nodes. Supports CPU, CUDA (GPU), and QPU (D-Wave) mining modes via compose profiles.

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
# Edit .env:
#   CERT_EMAIL  — set to enable automatic Let's Encrypt TLS (requires public_host as DNS name,
#                 plus uncommenting the 80/443 port bindings in docker-compose.yml — see "Miner-only vs full node" below)
#   DWAVE_API_KEY — D-Wave API token (QPU only)
```

TLS certificates are managed automatically by certbot inside the container. When `CERT_EMAIL` is set and `public_host` is a DNS name, the entrypoint obtains a Let's Encrypt certificate on startup and renews daily. For DNS-01 challenges, custom ACME providers, or other advanced options, see [TLS.md](https://gitlab.com/quip.network/quip-protocol/-/blob/main/docker/TLS.md).

### 4. Start

```bash
# CPU node
docker compose --profile cpu up -d

# CUDA GPU node
docker compose --profile cuda up -d

# QPU node
docker compose --profile qpu up -d
```

### Miner-only vs full node

By default the compose file exposes only the QUIP protocol port (20049). The REST interface (80/443) is commented out so miner-only nodes don't conflict with other services on the host. To run a full node — or to let the entrypoint obtain a Let's Encrypt certificate — uncomment the `80:80/tcp` and `443:443/tcp` lines in `docker-compose.yml` for your profile (cpu, cuda, or qpu). Exposing the REST interface will be required for full nodes in a future release.

### 5. Auto-updates (recommended)

Install an hourly cron job that checks for new images and recreates the container only when the digest changes:

```bash
./cron.sh --install    # install the hourly cron job
./cron.sh --uninstall  # remove it
./cron.sh              # run a one-off update check
```

`pull_policy: always` in the compose file ensures the registry is checked each time. If the image hasn't changed, `up -d` is a no-op — no restart, no downtime. Logs are written to `data/update.log`.

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
| View auto-update logs | `tail -f /var/log/quip-update.log` |
| Restart after config change | `docker compose restart cpu` |
| Restart after .env change | `docker compose --profile cpu up -d --force-recreate` |
| Force pull and redeploy | `docker compose pull cpu && docker compose up -d cpu` |
| Stop everything | `docker compose --profile cpu down` |

## Files

| File | Purpose |
|------|---------|
| `docker-compose.yml` | Node services (cpu/cuda/qpu profiles) |
| `data/config.toml` | Active node configuration (copied from a template) |
| `data/config.cpu.toml` | CPU mode template |
| `data/config.cuda.toml` | CUDA GPU mode template |
| `data/config.qpu.toml` | QPU (D-Wave) mode template |
| `.env` | CERT_EMAIL + DWAVE_API_KEY (not checked in) |
| `env.example` | Template for `.env` |
