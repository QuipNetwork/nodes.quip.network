#!/usr/bin/env bash
set -euo pipefail

# Auto-update quip node containers by pulling latest images and recreating
# only when the digest changes. Installs itself as an hourly cron job.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
LOG_FILE="${SCRIPT_DIR}/data/update.log"

# Print the --profile arguments needed to recreate whatever the operator is
# currently running, one flag pair per line. The validator profile is
# selected based on whether quip-validator is alongside the miner; the
# faucet profile is layered additively when quip-faucet is also up.
detect_profile() {
    local running
    running=$(docker ps --format '{{.Names}}')
    local has_validator=0
    grep -q "^quip-validator$" <<<"${running}" && has_validator=1

    if grep -q "^quip-cpu$" <<<"${running}"; then
        if [[ ${has_validator} -eq 1 ]]; then
            printf -- '--profile\nvalidator-cpu\n'
        else
            printf -- '--profile\ncpu\n'
        fi
    elif grep -q "^quip-cuda$" <<<"${running}"; then
        if [[ ${has_validator} -eq 1 ]]; then
            printf -- '--profile\nvalidator-cuda\n'
        else
            printf -- '--profile\ncuda\n'
        fi
    fi

    if grep -q "^quip-faucet$" <<<"${running}"; then
        printf -- '--profile\nfaucet\n'
    fi
}

update() {
    local profile_args=()
    mapfile -t profile_args < <(detect_profile)
    if [[ ${#profile_args[@]} -eq 0 ]]; then
        echo "$(date -Iseconds) No running quip container found, skipping"
        return 0
    fi

    echo "$(date -Iseconds) Checking for updates (${profile_args[*]})"
    docker compose -f "${COMPOSE_FILE}" "${profile_args[@]}" up -d
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
