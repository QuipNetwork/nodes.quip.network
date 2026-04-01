# Quip Network Node - Docker Deployment

Quick-start Docker Compose deployment for Quip Network nodes with Watchtower auto-updates. Supports CPU, CUDA (GPU), and QPU (D-Wave) mining modes via compose profiles.

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
#   CERT_EMAIL  — set to enable automatic Let's Encrypt TLS (requires public_host as DNS name + port 80 open)
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

Watchtower polls the registry every 5 minutes and automatically restarts the node when a new image is pushed.

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
| View watchtower logs | `docker compose logs -f watchtower` |
| Restart after config change | `docker compose restart cpu` |
| Restart after .env change | `docker compose --profile cpu up -d --force-recreate` |
| Force pull and redeploy | `docker compose pull cpu && docker compose up -d cpu` |
| Stop everything | `docker compose --profile cpu down` |

## Files

| File | Purpose |
|------|---------|
| `docker-compose.yml` | Node services (cpu/cuda/qpu profiles) + Watchtower |
| `data/config.toml` | Active node configuration (copied from a template) |
| `data/config.cpu.toml` | CPU mode template |
| `data/config.cuda.toml` | CUDA GPU mode template |
| `data/config.qpu.toml` | QPU (D-Wave) mode template |
| `.env` | CERT_EMAIL + DWAVE_API_KEY (not checked in) |
| `env.example` | Template for `.env` |
