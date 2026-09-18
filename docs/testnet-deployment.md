# Aglais — Bootnode Operator Host Deployment

Aglais is the Quip test network, in the way Sepolia is the Ethereum test network. This document describes the operator-host setup for running one of the three canonical Aglais bootnode validators (`bootnode-{1,2,3}.aglais.quip.network`). It does **not** cover end-user / miner setups — those are documented in the main [`README.md`](../README.md).

This repo is infrastructure-as-code. No operator secrets (mnemonics, node keys) live in git — they stay on the operator's host and are mounted into the validator container at runtime.

## Prerequisites

| Item | Value |
|---|---|
| Image | The tag `CHANNEL` names, `beta` by default. Both channels are Aglais, and the validator publishes no non-rc v0.3 build yet, so they resolve to the same image. Run `make show-channel` to resolve what you will pull. `latest` is pre-Aglais. |
| Chain spec | `chain-specs/aglais-network.json` (committed; same file every operator uses) |
| Base path | `data/aglais-chain-db` (mounted at `/data` in the validator container) |
| Compose v2.20+ | required for `depends_on.required: false` |
| Inbound ports | `30333/tcp+udp` (libp2p p2p), `80/tcp`+`443/tcp`+`20049/tcp` (Caddy, in the dashboard container: ACME + RPC + dashboard) |

## DNS

The chain spec embeds three bootnode multiaddrs:

```
/dns4/bootnode-1.aglais.quip.network/tcp/30333/p2p/12D3KooWBdhB4xGX6hfFsNufqQsG99kekiH9kJhLSiui3RgatnpE
/dns4/bootnode-2.aglais.quip.network/tcp/30333/p2p/12D3KooWPJAHo45AA94u3fYS3tXvyKouZnWihQnXWPHAzikXLfPW
/dns4/bootnode-3.aglais.quip.network/tcp/30333/p2p/12D3KooWM6n7wYvett975UnLYXrvnBGqLk2DLJoCRoFxgXTkptWe
```

The `dns4` resolution path means each bootnode needs an A record pointing at the operator host running that slot's validator. The peer id in the multiaddr is the **libp2p identity** derived from the operator's `node-key` file — it must match the key mounted into the container.

DNS is operator-managed; this repo does not provision A records.

## Node key

Each bootnode operator holds the private libp2p key whose hash matches the peer id embedded in the chain spec. The key is generated once (offline) by the network operator who derives all three slots, then distributed to the bootnode operators via the procedure in [`quip-validator/docs/testnet-keys.md`](https://gitlab.com/quip.network/quip-validator/-/blob/main/docs/testnet-keys.md).

On the host, place the key at `./data/aglais-chain-db/node-key`, which is `/data/node-key` inside the validator container (the directory is gitignored). Pass it with the `--node-key-file` flag. The peer ids did not change at the Aglais relaunch, so an operator moving from the previous testnet copies the key from the old base path:

```bash
mkdir -p data/aglais-chain-db
cp data/validator-data/node-key data/aglais-chain-db/node-key
```

```bash
# Verify the on-disk key matches the published peer id (run on the host)
docker run --rm -v "$PWD/data/aglais-chain-db:/data:ro" \
  --entrypoint /usr/local/bin/quip-network-node \
  registry.gitlab.com/quip.network/quip-validator/quip-network-node:v0.3.0-rc1 \
  key inspect-node-key --file /data/node-key
# Output: 12D3KooW... — must match this slot's peer id in chain-specs/aglais-network.json
```

## Validator command additions

The base compose stack runs the validator with stock flags. Bootnode operators need to:

1. Add `--node-key-file=/data/node-key` to the validator command in `docker-compose.yml` (or pass via a compose override file). This pins the libp2p identity to the operator's key.
2. Set `VALIDATOR_NAME` in `.env` to a stable, public-facing name (e.g., `bootnode-1`). It surfaces on the substrate telemetry feed.
3. Set `QUIP_HOSTNAME` to the bootnode's DNS name in the comma-separated production form so Caddy auto-TLS covers both `:443` and `:20049`:
   ```bash
   QUIP_HOSTNAME=bootnode-1.aglais.quip.network, bootnode-1.aglais.quip.network:20049
   CERT_EMAIL=ops@example.com
   ```

4. Expect the first start to wait, because the miner and the faucet gate on the validator's healthcheck, which passes once the node reaches the chain head. The dashboard skips that gate and starts at once, and its indexer applies its own sync gate instead so it does not index a partial chain. On a fresh host that wait covers the whole initial sync. See [Initial sync](../README.md#initial-sync) in the main guide.

## Session keys (BABE / GRANDPA)

After the validator's first boot, insert hybrid BABE and GRANDPA keys derived from the operator's session mnemonic. The Aglais genesis pins the rotated H4/H2 (FN-DSA-512) public keys that each operator derived from the existing mnemonic. The keystore lives under the new base path, so keys inserted for the previous testnet are not visible to the Aglais validator: insert them again. Procedure from [`quip-validator/docs/testnet-keys.md`](https://gitlab.com/quip.network/quip-validator/-/blob/main/docs/testnet-keys.md), executed inside the validator container so the keystore mount picks them up:

```bash
docker compose exec quip-validator \
  quip-network-node insert-hybrid-key \
    --base-path /data \
    --chain /etc/quip/chain-spec.json \
    --scheme hybrid-babe-h444 \
    --suri "<bip39-mnemonic-here>"

docker compose exec quip-validator \
  quip-network-node insert-hybrid-key \
    --base-path /data \
    --chain /etc/quip/chain-spec.json \
    --scheme hybrid-grandpa-h244 \
    --suri "<bip39-mnemonic-here>"
```

`insert-hybrid-key` derives the key-type id from the scheme (`babe` for H444, `gran` for H244). `--suri` also accepts the path of a file that holds the mnemonic. The stock `key insert --scheme sr25519` and `--scheme ed25519` commands produce pre-Aglais keys that the runtime 117 genesis does not accept.

Restart the validator after inserting both keys:

```bash
docker compose --profile cpu restart quip-validator
```

The session keys are then visible via `author_hasSessionKeys` over RPC.

> ⚠️ The mnemonic is sensitive material. Use a single shell session, do not echo it, and do not let it land in shell history.

## Ports recap

| Port | Direction | Purpose |
|---|---|---|
| `30333/tcp` | inbound (public) | libp2p TCP transport |
| `30333/udp` | inbound (public) | libp2p QUIC transport |
| `80/tcp` | inbound (public) | ACME HTTP-01 challenge + redirect to `:443` |
| `443/tcp` | inbound (public) | Caddy HTTPS (dashboard + `/api/v1/*` + `/rpc/*` + `/api/faucet/*`) |
| `20049/tcp` | inbound (public) | Same routes as `:443`, both bindings share one Let's Encrypt cert |
| `9944/tcp` | internal only | Substrate RPC (Caddy proxies; not host-published) |
| `9615/tcp` | internal only | Substrate Prometheus metrics (Caddy doesn't proxy yet) |

After bringing the validator up, verify every public-inbound port from the operator host via [`check.quip.network`](https://check.quip.network). The service self-checks using the caller's source IP (no host parameter), so curl from the host whose ports you want validated:

```bash
for p in 30333 80 443 20049; do
  curl -sS "https://check.quip.network/checkport?port=$p"
  echo
done
```

Bootnodes that fail the `:30333` check will keep gossiping outbound but no one can dial them — peer counts on other validators drop and the testnet loses redundancy. Treat a `"reachable": false` for `:30333` as on-call-grade.

## See also

- [`README.md`](../README.md) — main deployment guide (miners + non-bootnode operators)
- [`quip-validator/docs/genesis-quip-testnet.md`](https://gitlab.com/quip.network/quip-validator/-/blob/main/docs/genesis-quip-testnet.md) — full authorities + sudo + key procedure
- [`quip-validator/docs/testnet-keys.md`](https://gitlab.com/quip.network/quip-validator/-/blob/main/docs/testnet-keys.md) — operator key derivation + insertion procedure
