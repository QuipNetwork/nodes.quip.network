"""Seed a fresh Quip chain with advantage2_system1 as DefaultTopology + difficulty.

`quip-miner bootstrap --seed-chain` only supports zephyr Z(m,t) graphs for the
seed topology, and once `DefaultTopology` is set on chain there's no extrinsic
to swap it (`register_topology` only writes the default when none exists).
This script registers advantage2_system1 (4578 nodes / 41531 edges) FIRST on a
freshly-booted chain so it becomes the default, then seeds difficulty in the
same pass. Operators then run `quip-miner bootstrap` (no `--seed-chain`) to
register the miner + fund it via the faucet.

Run inside the miner image so HybridSigner + SubstrateClient + dwave_topologies
+ shared.miner_bootstrap are all on the path. Local dev example (//Alice sudo):

    docker compose --profile validator-cpu run --rm \\
        -v $(pwd)/scripts/seed-advantage2-topology.py:/seed.py:ro \\
        --entrypoint python3 cpu /seed.py --sudo-key //Alice

Testnet example (real sudo master seed from `key inspect`):

    docker compose --profile validator-cpu run --rm \\
        -v $(pwd)/scripts/seed-advantage2-topology.py:/seed.py:ro \\
        --entrypoint python3 cpu /seed.py \\
        --sudo-key 0xe5be9a5092b81bca64be81d212e7f2f9eba183bb7a90954f7b76361f6edb5c0a
"""

import argparse
import asyncio
import sys

from dwave_topologies.topologies.advantage2_system1 import ADVANTAGE2_SYSTEM1_TOPOLOGY

from shared.hybrid_signer import HybridSigner
from shared.miner_bootstrap import (
    DEFAULT_SEED_DIFFICULTY,
    _DEFAULT_ALLOWED_H,
    _DEFAULT_ALLOWED_J,
    _DEFAULT_ALLOWED_SPIN,
    _difficulty_to_dict,
    _resolve_dev_signer,
    _sudo_call,
    scale_dict,
)
from shared.substrate_client import SubstrateClient


def _build_signer(sudo_key: str) -> HybridSigner:
    """Build a HybridSigner from either a dev URI (//Alice) or a hex master seed.

    The substrate URI scheme (//Alice, //Bob, //Alice//stash) maps to precomputed
    32-byte master seeds in `shared.miner_bootstrap.DEV_HYBRID_SEEDS`; for
    production sudo keys, pass the 32-byte master seed as hex (with or without
    the `0x` prefix) — same value as substrate's `key inspect --output-type json`
    reports under `secretSeed` after hybrid extension.
    """
    if sudo_key.startswith("//"):
        return _resolve_dev_signer(sudo_key)
    raw = sudo_key.removeprefix("0x")
    if len(raw) != 64:
        raise ValueError(
            f"--sudo-key must be a dev URI (e.g. //Alice) or a 32-byte hex "
            f"master seed (64 hex chars, got {len(raw)})"
        )
    try:
        seed = bytes.fromhex(raw)
    except ValueError as exc:
        raise ValueError(f"--sudo-key hex decode failed: {exc}") from exc
    return HybridSigner.from_master_seed(seed)


async def main(args: argparse.Namespace) -> int:
    signer = _build_signer(args.sudo_key)
    print(f"sudo signer: {signer.ss58_address()}")

    g = ADVANTAGE2_SYSTEM1_TOPOLOGY.graph
    nodes = sorted(int(n) for n in g.nodes)
    edges = sorted((min(int(a), int(b)), max(int(a), int(b))) for a, b in g.edges)
    print(
        f"advantage2_system1: {len(nodes)} nodes (max_label={max(nodes)}), "
        f"{len(edges)} edges"
    )

    client = SubstrateClient(url=args.validator)
    await client.connect()
    print(f"connected to {args.validator}")

    iface = client._iface
    current = await client._run(lambda: iface.query("QuantumPow", "DefaultTopology"))
    if current is not None and current.value is not None:
        print(
            f"ERROR: DefaultTopology already set to {current.value}; "
            "wipe data/validator-data/chains and restart the validator first.",
            file=sys.stderr,
        )
        return 1

    print("submitting Sudo.sudo(QuantumPow.register_topology) ...")
    await _sudo_call(
        client,
        signer,
        "QuantumPow",
        "register_topology",
        {
            "nodes": (nodes,),
            "edges": (edges,),
            "allowed_h_values": scale_dict(_DEFAULT_ALLOWED_H),
            "allowed_j_values": scale_dict(_DEFAULT_ALLOWED_J),
            "allowed_spin_values": scale_dict(_DEFAULT_ALLOWED_SPIN),
        },
    )
    print("advantage2_system1 topology registered — block-included")

    print("submitting Sudo.sudo(QuantumPow.set_difficulty) ...")
    await _sudo_call(
        client,
        signer,
        "QuantumPow",
        "set_difficulty",
        {"difficulty": _difficulty_to_dict(DEFAULT_SEED_DIFFICULTY)},
    )
    print("difficulty seeded — block-included")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").split("\n\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--sudo-key",
        required=True,
        help="Sudo signer: dev URI (//Alice, //Bob) or 32-byte master seed as hex",
    )
    parser.add_argument(
        "--validator",
        default="ws://quip-validator:9944",
        help="Substrate validator WebSocket URL (default: %(default)s)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse_args())))
