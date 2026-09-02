#!/usr/bin/env bash
# Report whether the validator has finished its initial sync.
#
# Compose gates the miner and the dashboard on this check. Both read the chain
# through the validator's RPC, and both misbehave against a node that is still
# catching up: the miner's coordinator preflight reads the runtime at the node's
# best block, so a node at genesis reports the genesis runtime (`quip/103`,
# `QuantumPowApi` v1) and the coordinator refuses to drive it, while the
# dashboard indexer scans from genesis and caches an empty chain as the network.
#
# The node ships no HTTP client (no curl, no wget), so this speaks JSON-RPC over
# bash's /dev/tcp instead of adding a dependency to the image.
#
# Healthy means: not major-syncing, and either connected to peers or running a
# chain that expects none. The second arm is what keeps the localdev `--chain=dev`
# stack working, where Alice authors alone and `peers` is 0 forever.
#
#   {"peers":4,"isSyncing":false,"shouldHavePeers":true}   -> healthy (at head)
#   {"peers":0,"isSyncing":false,"shouldHavePeers":true}   -> unhealthy (no peers)
#   {"peers":6,"isSyncing":true,"shouldHavePeers":true}    -> unhealthy (catching up)
#   {"peers":0,"isSyncing":false,"shouldHavePeers":false}  -> healthy (dev chain)

set -euo pipefail

host=127.0.0.1
port="${QUIP_RPC_PORT:-9944}"
body='{"jsonrpc":"2.0","id":1,"method":"system_health","params":[]}'

if ! exec 3<>"/dev/tcp/${host}/${port}"; then
  echo "rpc unreachable on ${host}:${port}" >&2
  exit 1
fi

# Substrate's RPC host allowlist matches the full host:port authority, so the
# port has to be in the Host header — `Host: 127.0.0.1` alone is rejected with
# "Provided Host header is not whitelisted" on a node that has not been given
# --unsafe-rpc-external.
printf 'POST / HTTP/1.1\r\nHost: %s:%s\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s' \
  "$host" "$port" "${#body}" "$body" >&3

# The server closes the connection after answering (Connection: close), so cat
# terminates on EOF. The timeout covers a node that accepts the socket but does
# not answer, which would otherwise hang until docker kills the check.
response=$(timeout 5 cat <&3)
exec 3<&-

[[ $response =~ \"peers\":([0-9]+) ]] || {
  echo "no peer count in rpc response: ${response##*$'\r\n\r\n'}" >&2
  exit 1
}
peers="${BASH_REMATCH[1]}"

[[ $response =~ \"isSyncing\":(true|false) ]] || {
  echo "no sync flag in rpc response: ${response##*$'\r\n\r\n'}" >&2
  exit 1
}
syncing="${BASH_REMATCH[1]}"

[[ $response =~ \"shouldHavePeers\":(true|false) ]] || {
  echo "no shouldHavePeers flag in rpc response: ${response##*$'\r\n\r\n'}" >&2
  exit 1
}
should_have_peers="${BASH_REMATCH[1]}"

state="peers=${peers} isSyncing=${syncing} shouldHavePeers=${should_have_peers}"

if [[ $syncing == true ]]; then
  echo "catching up to the chain head (${state})" >&2
  exit 1
fi

if [[ $peers -eq 0 && $should_have_peers == true ]]; then
  echo "no peers; check that the chain spec matches the network (${state})" >&2
  exit 1
fi

echo "synced (${state})"
