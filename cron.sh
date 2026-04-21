#!/usr/bin/env bash
set -euo pipefail

# Auto-update quip node containers by pulling latest images and recreating
# only when the digest changes. Installs itself as an hourly cron job.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
LOG_FILE="${SCRIPT_DIR}/data/update.log"

detect_profile() {
    local running
    running=$(docker ps --format '{{.Names}}')
    local suffix=""
    if ! grep -q "^quip-dashboard$" <<<"${running}"; then
        # No dashboard → caddy can't be meaningful either; use the -nodash
        # profile which omits dashboard, postgres, and caddy.
        suffix="-nodash"
    elif ! grep -q "^quip-caddy$" <<<"${running}"; then
        # Dashboard is running but caddy isn't → operator picked -notls.
        suffix="-notls"
    fi
    for profile in cuda cpu qpu; do
        if grep -q "^quip-${profile}$" <<<"${running}"; then
            echo "${profile}${suffix}"
            return
        fi
    done
}

update() {
    local profile
    profile=$(detect_profile)
    if [[ -z "${profile}" ]]; then
        echo "$(date -Iseconds) No running quip container found, skipping"
        return 0
    fi

    echo "$(date -Iseconds) Checking for updates (profile: ${profile})"
    docker compose -f "${COMPOSE_FILE}" --profile "${profile}" up -d
}

install() {
    local cron_entry="0 * * * * ${SCRIPT_DIR}/cron.sh >>${LOG_FILE} 2>&1"
    local cron_marker="# quip-node-update"

    (crontab -l 2>/dev/null | grep -v "${cron_marker}") | {
        cat
        echo "${cron_entry} ${cron_marker}"
    } | crontab -

    echo "Installed cron job (hourly):"
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
