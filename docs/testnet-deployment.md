# Quip Testnet — Operator Host Deployment

This document describes the operator-host setup for running one of the three canonical Quip Testnet bootnode validators (`bootnode-{1,2,3}.testnet.quip.network`). It does **not** cover end-user / miner setups — those are documented in the main [`README.md`](../README.md).

This repo is infrastructure-as-code. No operator secrets (mnemonics, node keys) live in git — they stay on the operator's host and are mounted into the validator container at runtime.

## Prerequisites

| Item | Value |
|---|---|
| Image | `registry.gitlab.com/quip.network/quip-protocol-rs/quip-network-node:v0.2-preview` |
| Image (SHA-pinned) | `:sha-13536ad2` (digest `sha256:42a22dc1…`) |
| Chain spec | `chain-specs/quip-testnet.json` (committed; same file every operator uses) |
| Compose v2.20+ | required for `depends_on.required: false` |
| Inbound ports | `30333/tcp+udp` (libp2p p2p), `80/tcp`+`443/tcp`+`20049/tcp` (Caddy: ACME + RPC + dashboard) |

## DNS

The chain spec embeds three bootnode multiaddrs:

```
/dns4/bootnode-1.testnet.quip.network/tcp/30333/p2p/12D3KooWBdhB4xGX6hfFsNufqQsG99kekiH9kJhLSiui3RgatnpE
/dns4/bootnode-2.testnet.quip.network/tcp/30333/p2p/12D3KooWPJAHo45AA94u3fYS3tXvyKouZnWihQnXWPHAzikXLfPW
/dns4/bootnode-3.testnet.quip.network/tcp/30333/p2p/12D3KooWM6n7wYvett975UnLYXrvnBGqLk2DLJoCRoFxgXTkptWe
```

The `dns4` resolution path means each bootnode needs an A record pointing at the operator host running that slot's validator. The peer id in the multiaddr is the **libp2p identity** derived from the operator's `node-key` file — it must match the key mounted into the container.

DNS is operator-managed; this repo does not provision A records.

## Node key

Each bootnode operator holds the private libp2p key whose hash matches the peer id embedded in the chain spec. The key is generated once (offline) by the network operator who derives all three slots, then distributed to the bootnode operators via the procedure in [`quip-protocol-rs/docs/testnet-keys.md`](https://gitlab.com/quip.network/quip-protocol-rs/-/blob/v0.2/docs/testnet-keys.md).

On the host, place the key at `./data/node-key` (already gitignored in this repo). Mount it via the `--node-key-file` flag.

```bash
# Verify the on-disk key matches the published peer id (run on the host)
docker run --rm -v "$PWD/data:/data:ro" \
  registry.gitlab.com/quip.network/quip-protocol-rs/quip-network-node:v0.2-preview \
  key inspect-node-key --file /data/node-key
# Output: 12D3KooW... — must match this slot's peer id in chain-specs/quip-testnet.json
```

## Validator command additions

The base compose stack runs the validator with stock flags. Bootnode operators need to:

1. Add `--node-key-file=/data/node-key` to the validator command in `docker-compose.yml` (or pass via a compose override file). This pins the libp2p identity to the operator's key.
2. Set `VALIDATOR_NAME` in `.env` to a stable, public-facing name (e.g., `bootnode-1`). It surfaces on the substrate telemetry feed.
3. Set `QUIP_HOSTNAME` to the bootnode's DNS name in the comma-separated production form so Caddy auto-TLS covers both `:443` and `:20049`:
   ```bash
   QUIP_HOSTNAME=bootnode-1.testnet.quip.network, bootnode-1.testnet.quip.network:20049
   CERT_EMAIL=ops@example.com
   ```

## Session keys (BABE / GRANDPA)

After the validator's first boot, insert hybrid BABE and GRANDPA keys derived from the operator's session mnemonic. Procedure from [`quip-protocol-rs/docs/testnet-keys.md`](https://gitlab.com/quip.network/quip-protocol-rs/-/blob/v0.2/docs/testnet-keys.md), executed inside the validator container so the keystore mount picks them up:

```bash
docker compose exec quip-validator \
  quip-network-node key insert \
    --base-path /data \
    --chain /etc/quip/chain-spec.json \
    --scheme sr25519 \
    --suri "<bip39-mnemonic-here>" \
    --key-type babe

docker compose exec quip-validator \
  quip-network-node key insert \
    --base-path /data \
    --chain /etc/quip/chain-spec.json \
    --scheme ed25519 \
    --suri "<bip39-mnemonic-here>" \
    --key-type gran
```

Restart the validator after inserting both keys:

```bash
docker compose --profile validator-cpu restart quip-validator
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

## Operator-2 compromise note

The genesis manifest in [`quip-protocol-rs/docs/genesis-quip-testnet.md`](https://gitlab.com/quip.network/quip-protocol-rs/-/blob/v0.2/docs/genesis-quip-testnet.md) documents that operator-2's mnemonic was exposed in a developer session prior to testnet tagging. The slot stays in testnet — 2-of-3 honest operators preserves byzantine tolerance — but the compromised key must be rotated before any mainnet derivation from this testnet.

Do not reuse operator-2 keys for any production / mainnet purpose.

## See also

- [`README.md`](../README.md) — main deployment guide (miners + non-bootnode operators)
- [`quip-protocol-rs/docs/genesis-quip-testnet.md`](https://gitlab.com/quip.network/quip-protocol-rs/-/blob/v0.2/docs/genesis-quip-testnet.md) — full authorities + sudo + key procedure
- [`quip-protocol-rs/docs/testnet-keys.md`](https://gitlab.com/quip.network/quip-protocol-rs/-/blob/v0.2/docs/testnet-keys.md) — operator key derivation + insertion procedure
