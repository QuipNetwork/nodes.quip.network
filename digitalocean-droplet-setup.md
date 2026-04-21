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
ufw allow 80/tcp       # Certbot HTTP-01 ACME challenge
ufw allow 443/tcp      # HTTPS REST API
ufw allow 20080/tcp     # Dashboard UI (restrict to your IP for private deployments)
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

# TLS certificates (injected by certbot in entrypoint.sh)
# tls_cert_file = "/data/certs/private/fullchain.pem"
# tls_key_file = "/data/certs/private/privkey.pem"

tofu = true
trust_db = "/data/trust.db"

# REST API (-1 = disabled)
# rest_insecure_port = 80 lets the bundled dashboard poll the node over the
# compose network; the 80:80/tcp host mapping in docker-compose.yml stays
# commented out so this is not exposed externally.
rest_host = "0.0.0.0"
rest_port = -1
rest_insecure_port = 80
```

### 4.4 Configure environment variables

```bash
cp env.example .env
chmod 600 .env
```

Edit `.env`:

```bash
# TLS: enables automatic Let's Encrypt certificates when public_host is a DNS name
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

Each command brings up the selected node plus `quip-dashboard` and `quip-postgres`. The dashboard UI is reachable at `http://<DROPLET_IP>:20080` (or lock it down to your IP with `ufw allow from <your-ip> to any port 20080` and `ufw delete allow 20080/tcp`). The dashboard polls the local node via the compose alias `quip-node`; the config templates ship with `rest_insecure_port = 80` already set so this works out of the box. Override `QUIP_NODE_URL` in `.env` if you prefer to point at a public full node.

To run without the dashboard, use the `-nodash` profile variant (`cpu-nodash`, `cuda-nodash`, `qpu-nodash`) — this skips the dashboard and Postgres services. `cron.sh` detects which variant is running and preserves it on update.

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

The Quip node image includes built-in certbot support. TLS activates automatically when:

1. `public_host` in `config.toml` is a DNS name (not an IP address)
2. `CERT_EMAIL` is set in `.env`

The entrypoint obtains a Let's Encrypt certificate on startup and installs a daily cron job for renewal. Certificates are stored in `/data/certs/private/`.

Port 80 must be reachable from the internet for the HTTP-01 ACME challenge (already open from step 3.3).

For DNS-01 challenges, custom ACME providers (ZeroSSL, Buypass), or other advanced options, see [TLS.md](https://gitlab.com/quip.network/quip-protocol/-/blob/main/docker/TLS.md).

---

## 7. Maintenance

| Task | Command |
|------|---------|
| View node logs | `docker compose logs -f cpu` (or `cuda`, `qpu`) |
| View dashboard logs | `docker compose logs -f dashboard` |
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
| View TLS certificates | `docker exec quip-cpu certbot certificates --config-dir /data/certs/certbot-config` |
| Force TLS renewal | `docker exec quip-cpu /data/certs/certbot mynode.example.com` |
| Check cron | `docker exec quip-cpu busybox crontab -l` |

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
