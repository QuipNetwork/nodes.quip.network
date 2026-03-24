#!/usr/bin/env bash
set -euo pipefail

# Auto-update quip node containers by pulling latest images and recreating
# if changed. Installs itself as a cron job when run with --install.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
LOG_FILE="/var/log/quip-update.log"

update() {
    local profile
    profile=$(detect_profile)
    if [[ -z "${profile}" ]]; then
        echo "$(date -Iseconds) No active quip profile found, skipping" >>"${LOG_FILE}"
        return 0
    fi

    {
        echo "$(date -Iseconds) Checking for updates (profile: ${profile})"
        docker compose -f "${COMPOSE_FILE}" --profile "${profile}" pull
        docker compose -f "${COMPOSE_FILE}" --profile "${profile}" up -d
    } >>"${LOG_FILE}" 2>&1
}

detect_profile() {
    for profile in cuda cpu qpu; do
        if docker ps --format '{{.Names}}' | grep -q "^quip-${profile}$"; then
            echo "${profile}"
            return
        fi
    done
}

install() {
    local cron_entry="*/5 * * * * ${SCRIPT_DIR}/cron.sh >>/var/log/quip-update.log 2>&1"
    local cron_marker="# quip-node-update"

    # Remove existing entry if present, then add fresh
    (crontab -l 2>/dev/null | grep -v "${cron_marker}") | {
        cat
        echo "${cron_entry} ${cron_marker}"
    } | crontab -

    echo "Installed cron job (every 5 minutes):"
    echo "  ${cron_entry}"
    echo "Logs: ${LOG_FILE}"
}

uninstall() {
    local cron_marker="# quip-node-update"
    crontab -l 2>/dev/null | grep -v "${cron_marker}" | crontab -
    echo "Removed quip-node-update cron job"
}

case "${1:-}" in
    --install) install ;;
    --uninstall) uninstall ;;
    *) update ;;
esac
