# DigitalOcean Droplet Setup for Quip Network Nodes

## Overview

This guide sets up a DigitalOcean Droplet that:

1. Points your DNS at a static IP
2. Runs a Quip Network node via Docker Compose
3. Auto-updates the node image via a cron job

---

## 1. Create the Droplet

### Recommended Spec

- **Image:** Ubuntu 24.04 LTS
- **Plan:** Basic, 2 GB RAM / 1 vCPU minimum (scale as needed)
- **Region:** Closest to your users
- **Authentication:** SSH key (do not use password auth)
- **Networking:** Enable the free DigitalOcean firewall

### Via the DigitalOcean CLI

```bash
doctl compute droplet create my-quip-node \
  --region nyc1 \
  --size s-1vcpu-2gb \
  --image ubuntu-24-04-x64 \
  --ssh-keys <your-ssh-key-fingerprint> \
  --wait
```

Note the **IPv4 address** from the output — you'll need it for DNS.

---

## 2. DNS Setup

At your DNS provider, create an **A record** pointing to the Droplet's IP:

| Type | Name                 | Value          | TTL |
|------|----------------------|----------------|-----|
| A    | mynode.example.com   | `<DROPLET_IP>` | 300 |

DNS propagation typically takes a few minutes to a few hours.

---

## 3. Initial Server Setup

SSH into the Droplet and run through these one-time steps:

```bash
ssh root@<DROPLET_IP>
```

### 3.1 System Updates

```bash
apt update && apt upgrade -y
```

### 3.2 Create a Non-Root User

```bash
adduser deploy
usermod -aG sudo deploy

# Copy SSH keys to the new user
rsync --archive --chown=deploy:deploy ~/.ssh /home/deploy

# From now on, SSH in as: ssh deploy@<DROPLET_IP>
```

### 3.3 Configure the Firewall

```bash
ufw allow OpenSSH
ufw allow 20049/udp    # QUIC peer-to-peer
ufw allow 20049/tcp    # QUIC peer-to-peer
ufw allow 80/tcp       # Caddy HTTP + Let's Encrypt HTTP-01 challenge
ufw allow 443/tcp      # Caddy HTTPS (dashboard UI + node REST)
ufw enable
```

### 3.4 Install Docker & Docker Compose

Use Docker's official install script — this installs Docker Engine, CLI, and the Compose plugin (v2) together. Do **not** use `apt install docker.io` or `apt install docker-compose`, which are outdated Debian packages that lack Compose v2 and `pull_policy` support.

```bash
# Remove any old Debian-packaged Docker (if present)
sudo apt remove -y docker.io docker-compose 2>/dev/null

# Install Docker from the official repository
curl -fsSL https://get.docker.com | sh

# Add your user to the docker group (avoids needing sudo for docker commands)
sudo usermod -aG docker deploy

# Verify both Docker and Compose v2 are installed
docker --version
docker compose version   # Must show "Docker Compose version v2.x.x"
```

Log out and back in as `deploy` for the group change to take effect.

If `docker compose version` fails, install the plugin manually:

```bash
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m) \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
```

### 3.5 Tune Kernel Networking (recommended)

Enable BBR + fair-queueing + disable slow-start-after-idle on the host. These improve throughput for long-lived TCP connections (HTTP/2, node REST) and provide the packet pacing BBR requires:

```bash
cd ~/app   # after cloning in step 4.1
sudo ./scripts/sysctl-tune.sh
```

Idempotent; writes `/etc/sysctl.d/99-quip.conf`. Ubuntu 24.04 has all prerequisites.

---

## 4. Deploy the Node

### 4.1 Clone the deployment repo

```bash
git clone https://gitlab.com/quip.network/nodes.quip.network.git ~/app
cd ~/app
```

### 4.2 Choose a mode and copy the config template

```bash
# CPU mining
cp data/config.cpu.toml data/config.toml

# CUDA GPU mining (requires NVIDIA GPU + drivers)
cp data/config.cuda.toml data/config.toml

# QPU mining (D-Wave)
cp data/config.qpu.toml data/config.toml
```

### 4.3 Configure the node

Edit `data/config.toml`:

```toml
[global]
node_name = "my-node"
listen = "::"                               # Dual-stack IPv4+IPv6
port = 20049
public_host = "mynode.example.com"          # Your DNS name (enables certbot TLS)
secret = "CHANGE_ME"                        # Unique value for node identity

peer = [
    "qpu-1.nodes.quip.network:20049",
    "cpu-1.quip.carback.us:20049",
    "gpu-1.quip.carback.us:20049",
    "gpu-2.quip.carback.us:20050",
]

verify_tls = false

# TLS certificates for QUIC transport (optional; see TLS.md in quip-protocol
# for wiring real certs into QUIC — Caddy manages HTTP(S) certs separately).
# tls_cert_file = "/data/certs/private/fullchain.pem"
# tls_key_file = "/data/certs/private/privkey.pem"

tofu = true
trust_db = "/data/trust.db"

# REST API on port 80 inside the container. Caddy (on the compose network)
# fronts this; the host never sees port 80 of this container directly.
rest_host = "0.0.0.0"
rest_port = -1
rest_insecure_port = 80
```

### 4.4 Configure environment variables

```bash
cp env.example .env
printf 'PUID=%s\nPGID=%s\n' "$(id -u)" "$(id -g)" >> .env
chmod 600 .env
```

The `printf` line seeds `.env` with your host's uid/gid. Since quip-protocol v0.1.7 the node container runs as a non-root `quip` user and chowns `/data` to match `PUID`/`PGID`; aligning these with your `deploy` user's uid keeps files editable without `sudo`.

Edit `.env`:

```bash
# DNS name the droplet answers on. Caddy uses this for automatic Let's Encrypt
# TLS; must match the A record from step 2 and be reachable on port 80.
QUIP_HOSTNAME=mynode.example.com

# Email registered with Let's Encrypt (required when QUIP_HOSTNAME is a DNS name).
CERT_EMAIL=admin@example.com

# D-Wave API token (QPU only)
DWAVE_API_KEY=

# Optional: Postgres isn't exposed to the host, so the default ('quip') is fine
# for most deployments. Override for defense-in-depth on shared droplets.
# POSTGRES_PASSWORD=<strong-password>
```

### 4.5 Start the node

```bash
# CPU node
docker compose --profile cpu up -d

# CUDA GPU node
docker compose --profile cuda up -d

# QPU node
docker compose --profile qpu up -d
```

Each command brings up the selected node plus `quip-dashboard`, `quip-postgres`, and `quip-caddy`. Caddy provisions a Let's Encrypt cert for `QUIP_HOSTNAME` on first startup and serves both the dashboard SPA (at `/`) and node REST (at `/api/v1/*`) over HTTPS. Open your browser to `https://mynode.example.com/`.

Caddy's certs persist in the `quip-caddy-data` named volume; renewals happen automatically (no cron or sidecar needed).

To run without the dashboard, use the `-nodash` profile variant (`cpu-nodash`, `cuda-nodash`, `qpu-nodash`) — this skips the dashboard, Postgres, and Caddy. `cron.sh` detects which variant is running and preserves it on update.

### 4.6 Set up auto-updates

Add an hourly cron job to check for new images and recreate the container only when the digest changes. Replace `<profile>` with your mode (`cpu`, `cuda`, or `qpu`):

```bash
crontab -e

# Add this line (runs hourly at minute 0):
0 * * * * cd ~/app && docker compose --profile <profile> up -d >> /var/log/quip-update.log 2>&1
```

`docker compose up -d` is a no-op when the image hasn't changed — the node keeps running uninterrupted between actual updates.

---

## 5. Registry Authentication

The Quip node images are hosted on GitLab Container Registry. If the registry requires authentication:

```bash
docker login registry.gitlab.com
```

This creates `~/.docker/config.json`. The cron job inherits these credentials from the user's Docker config.

---

## 6. TLS Certificates

Caddy manages TLS for the HTTP(S) front-door automatically:

1. `QUIP_HOSTNAME` in `.env` is a DNS name that resolves to this droplet
2. `CERT_EMAIL` in `.env` is set
3. Port 80 is reachable from the internet (ACME HTTP-01 challenge — already open from step 3.3)

Caddy provisions a Let's Encrypt cert on first startup, serves HTTPS on 443, redirects HTTP→HTTPS, and renews on its own internal timer. Certificates persist in the `quip-caddy-data` named volume.

**QUIC transport TLS** on port 20049 (node-to-node peer traffic) is a separate concern. The default configuration uses TOFU + `trust.db` for peer identity. See [TLS.md](https://gitlab.com/quip.network/quip-protocol/-/blob/main/docker/TLS.md) in quip-protocol for wiring real certs into QUIC.

For DNS-01 challenges or custom ACME providers, see the [Caddy docs](https://caddyserver.com/docs/automatic-https).

---

## 7. Maintenance

| Task | Command |
|------|---------|
| View node logs | `docker compose logs -f cpu` (or `cuda`, `qpu`) |
| View dashboard logs | `docker compose logs -f dashboard` |
| View Caddy / TLS logs | `docker compose logs -f caddy` |
| View auto-update logs | `tail -f /var/log/quip-update.log` |
| Restart after config change | `docker compose restart cpu` |
| Restart after .env change | `docker compose --profile cpu up -d --force-recreate` |
| Force pull & redeploy | `docker compose pull cpu && docker compose up -d cpu` |
| Stop everything | `docker compose --profile cpu down` |
| Stop & remove volumes | `docker compose down -v` (destroys data) |
| Update server packages | `sudo apt update && sudo apt upgrade -y` |
| Check disk usage | `df -h && docker system df` |
| Prune unused Docker objects | `docker system prune -af` |
| Edit config | `nano data/config.toml && docker compose restart cpu` |
| Edit env vars | `nano .env && docker compose --profile cpu up -d --force-recreate` |
| Inspect cert (live) | `openssl s_client -connect mynode.example.com:443 -servername mynode.example.com -brief </dev/null` |
| Force cert renewal | `docker exec quip-caddy caddy reload --config /etc/caddy/Caddyfile` (normally not needed — Caddy renews on its own) |

---

## 8. Optional Enhancements

### Automated Backups

Cron job to back up the data directory to DigitalOcean Spaces (S3-compatible):

```bash
crontab -e
# Add:
0 3 * * * tar czf /tmp/quip-backup-$(date +\%F).tar.gz /home/deploy/app/data && \
  s3cmd put /tmp/quip-backup-*.tar.gz s3://my-backups/ && \
  rm /tmp/quip-backup-*.tar.gz
```

### Monitoring

- **DigitalOcean Monitoring:** Free, built-in — enable it on the Droplet dashboard for CPU/memory/disk alerts.
- **Container-level:** Use `docker stats` or add cAdvisor to the compose stack for per-container metrics.
