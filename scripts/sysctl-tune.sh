#!/usr/bin/env bash
# Install host kernel tuning for Quip node operators.
#
# - tcp_slow_start_after_idle=0: keep cwnd across idle periods (better for
#   long-lived keep-alive connections like HTTP/2 and QUIC peers)
# - tcp_congestion_control=bbr: better throughput on lossy/high-RTT paths
# - default_qdisc=fq: fair queueing; required for BBR's packet pacing
#
# Requires kernel >= 4.9 (every supported Ubuntu LTS qualifies).
# Idempotent: re-running overwrites the same file and re-applies sysctls.

set -euo pipefail

TARGET=/etc/sysctl.d/99-quip.conf

sudo tee "$TARGET" >/dev/null <<'EOF'
# Quip node host tuning
net.ipv4.tcp_slow_start_after_idle = 0
net.ipv4.tcp_congestion_control = bbr
net.core.default_qdisc = fq
EOF

sudo sysctl --system >/dev/null

echo "Applied $TARGET."
for k in net.ipv4.tcp_slow_start_after_idle \
         net.ipv4.tcp_congestion_control \
         net.core.default_qdisc; do
    printf '  %-40s %s\n' "$k" "$(sysctl -n "$k")"
done
