# DigitalOcean Droplet Setup with Docker & Watchtower

## Overview

This guide sets up a DigitalOcean Droplet that:

1. Points your DNS at a static IP
2. Manages configuration, data volumes, and environment variables via docker-compose
3. Auto-detects and deploys the latest Docker image using Watchtower

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
doctl compute droplet create my-app-server \
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

| Type | Name            | Value            | TTL  |
|------|-----------------|------------------|------|
| A    | app.example.com | `<DROPLET_IP>`   | 300  |

If you want a root domain, also add:

| Type | Name        | Value            | TTL  |
|------|-------------|------------------|------|
| A    | example.com | `<DROPLET_IP>`   | 300  |

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
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable
```

### 3.4 Install Docker & Docker Compose

```bash
# Install Docker
curl -fsSL https://get.docker.com | sh

# Add your user to the docker group (avoids needing sudo for docker commands)
usermod -aG docker deploy

# Verify
docker --version
docker compose version
```

Log out and back in as `deploy` for the group change to take effect.

---

## 4. Project Structure

Create a clean directory layout on the server:

```
/home/deploy/app/
├── docker-compose.yml      # Service definitions
├── .env                    # Environment variables (gitignored, secrets live here)
├── config/                 # Mounted config files
│   └── app.conf            # Your app's config (nginx, app settings, etc.)
└── data/                   # Persistent data volume
```

```bash
mkdir -p ~/app/config ~/app/data
cd ~/app
```

---

## 5. Docker Compose Configuration

### 5.1 Environment Variables

Create `~/app/.env`:

```bash
# App settings
APP_ENV=production
APP_PORT=8080
DATABASE_URL=postgres://user:password@db-host:5432/mydb
SECRET_KEY=change-me-to-something-random

# Registry credentials (if using a private registry)
REGISTRY_USER=myuser
REGISTRY_PASSWORD=mytoken
```

Lock down permissions:

```bash
chmod 600 ~/app/.env
```

### 5.2 docker-compose.yml

Create `~/app/docker-compose.yml`:

```yaml
services:
  # ---- Your Application ----
  app:
    image: myregistry/myapp:latest      # <-- Change to your image
    container_name: myapp
    restart: unless-stopped
    ports:
      - "80:${APP_PORT:-8080}"
    env_file:
      - .env
    volumes:
      - ./config:/app/config:ro          # Config files (read-only)
      - ./data:/app/data                 # Persistent data (read-write)
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:${APP_PORT:-8080}/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    labels:
      - "com.centurylinklabs.watchtower.enable=true"

  # ---- Watchtower (auto-updater) ----
  watchtower:
    image: containrrr/watchtower
    container_name: watchtower
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      # If using a private registry, mount Docker credentials:
      # - /home/deploy/.docker/config.json:/config.json:ro
    environment:
      # Poll every 5 minutes (300 seconds)
      - WATCHTOWER_POLL_INTERVAL=300
      # Only update containers with the watchtower label
      - WATCHTOWER_LABEL_ENABLE=true
      # Remove old images after update
      - WATCHTOWER_CLEANUP=true
      # Optional: send notifications on update
      # - WATCHTOWER_NOTIFICATIONS=slack
      # - WATCHTOWER_NOTIFICATION_SLACK_HOOK_URL=https://hooks.slack.com/services/xxx
    labels:
      - "com.centurylinklabs.watchtower.enable=true"
```

**Key design decisions in this file:**

- `WATCHTOWER_LABEL_ENABLE=true` ensures Watchtower only touches containers you explicitly label, so it won't interfere with anything else you run on the box.
- Config is mounted read-only (`:ro`) so the app can't accidentally modify its own config.
- Data is mounted read-write for persistence across container restarts and updates.
- The healthcheck gives Watchtower and Docker a way to know if the new image is actually working.

---

## 6. Private Registry Authentication (If Needed)

If your image is in a private registry, log in so Watchtower can pull:

```bash
docker login ghcr.io          # or docker.io, your-registry.com, etc.
```

This creates `~/.docker/config.json`. Uncomment the volume mount in the Watchtower service above to share these credentials.

---

## 7. Start Everything

```bash
cd ~/app
docker compose up -d
```

Verify:

```bash
# Check containers are running
docker compose ps

# Check logs
docker compose logs -f app
docker compose logs -f watchtower
```

---

## 8. TLS Certificates

The Quip node image includes built-in certbot support. TLS activates automatically when:

1. `public_host` in `config.toml` is a DNS name (not an IP address)
2. `CERT_EMAIL` is set in `.env`

The entrypoint obtains a Let's Encrypt certificate on startup and installs a daily cron job for renewal. Certificates are stored in `/data/certs/private/`.

```bash
# In ~/app/.env, add:
CERT_EMAIL=admin@example.com
```

Port 80 must be reachable from the internet for the HTTP-01 ACME challenge (already open from the firewall setup in step 3.3).

For DNS-01 challenges, custom ACME providers (ZeroSSL, Buypass), or other advanced options, see [TLS.md](https://gitlab.com/piqued/quip-protocol/-/blob/main/docker/TLS.md).

---

## 9. Deploying Updates

The workflow is now:

1. **Build and push** your image in CI (GitHub Actions, GitLab CI, etc.):
   ```bash
   docker build -t myregistry/myapp:latest .
   docker push myregistry/myapp:latest
   ```
2. **Watchtower detects** the new digest within the poll interval (default 5 min).
3. **Watchtower pulls** the new image, stops the old container, starts a new one with the same config.
4. **Healthcheck confirms** the new container is serving traffic.

### Manual Deploy (Skip the Wait)

If you don't want to wait for the poll interval:

```bash
cd ~/app
docker compose pull app
docker compose up -d app
```

---

## 10. Maintenance Cheat Sheet

| Task                        | Command                                        |
|-----------------------------|-------------------------------------------------|
| View running containers     | `docker compose ps`                            |
| View app logs               | `docker compose logs -f app`                   |
| View watchtower logs        | `docker compose logs -f watchtower`            |
| Restart app                 | `docker compose restart app`                   |
| Force pull & redeploy       | `docker compose pull app && docker compose up -d app` |
| Stop everything             | `docker compose down`                          |
| Stop & remove volumes       | `docker compose down -v` ⚠️ destroys data      |
| Update server packages      | `sudo apt update && sudo apt upgrade -y`       |
| Check disk usage            | `df -h && docker system df`                    |
| Prune unused Docker objects | `docker system prune -af`                      |
| Edit env vars               | `nano ~/app/.env && docker compose up -d`      |
| Edit config                 | `nano ~/app/config/app.conf && docker compose restart app` |

---

## 11. Optional Enhancements

### Watchtower Notifications

Get a Slack/email alert whenever Watchtower deploys:

```yaml
# In watchtower environment:
- WATCHTOWER_NOTIFICATIONS=slack
- WATCHTOWER_NOTIFICATION_SLACK_HOOK_URL=https://hooks.slack.com/services/T00/B00/xxxx
```

### Automated Backups

Cron job to back up the data directory to DigitalOcean Spaces (S3-compatible):

```bash
# Install s3cmd or use rclone, then:
crontab -e
# Add:
0 3 * * * tar czf /tmp/app-backup-$(date +\%F).tar.gz /home/deploy/app/data && \
  s3cmd put /tmp/app-backup-*.tar.gz s3://my-backups/ && \
  rm /tmp/app-backup-*.tar.gz
```

### Monitoring

- **DigitalOcean Monitoring:** Free, built-in — enable it on the Droplet dashboard for CPU/memory/disk alerts.
- **UptimeRobot or Healthchecks.io:** Free external uptime monitoring — point it at `https://app.example.com/health`.
- **Container-level:** Use `docker stats` or add cAdvisor to the compose stack for per-container metrics.
