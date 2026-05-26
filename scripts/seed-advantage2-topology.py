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

    docker compose --profile cpu run --rm \\
        -v $(pwd)/scripts/seed-advantage2-topology.py:/seed.py:ro \\
        --entrypoint python3 cpu /seed.py --sudo-key //Alice

Testnet example (operator mnemonic, mounted read-only into the container):

    docker compose -f docker-compose.yml --profile cpu run --rm \\
        -v $(pwd)/scripts/seed-advantage2-topology.py:/seed.py:ro \\
        -v /path/to/quip-testnet-keys/operator-1/mnemonic:/keys/mnemonic:ro \\
        --entrypoint python3 cpu /seed.py --mnemonic-file /keys/mnemonic

Testnet alternative (32-byte hybrid master seed as hex):

    docker compose -f docker-compose.yml --profile cpu run --rm \\
        -v $(pwd)/scripts/seed-advantage2-topology.py:/seed.py:ro \\
        --entrypoint python3 cpu /seed.py \\
        --sudo-key 0xe5be9a5092b81bca64be81d212e7f2f9eba183bb7a90954f7b76361f6edb5c0a
"""

import argparse
import asyncio
import sys
from pathlib import Path

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
from substrate.client import SubstrateClient


def _signer_from_hex(sudo_key: str) -> HybridSigner:
    """Build a HybridSigner from a dev URI (//Alice) or a 32-byte hex master seed.

    The substrate URI scheme (//Alice, //Bob, //Alice//stash) maps to precomputed
    32-byte master seeds in `shared.miner_bootstrap.DEV_HYBRID_SEEDS`; for
    production sudo keys, pass the 32-byte master seed as hex (with or without
    the `0x` prefix) — same value as substrate's `key inspect --output-type json`
    reports under `secretSeed`. (The hybrid signer uses this same 32-byte
    BIP39 mini-secret-key as input to both the sr25519 and ML-DSA-44 key
    generators — see `HybridSigner.from_master_seed`.)
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


def _signer_from_mnemonic_file(path: Path) -> HybridSigner:
    """Build a HybridSigner from a BIP39 mnemonic file (one phrase, trimmed).

    Uses substrate-interface's `Keypair.create_from_mnemonic` to derive the
    32-byte BIP39 mini-secret-key, which is the same seed substrate's
    `key inspect` reports for the same mnemonic. That seed is then fed into
    `HybridSigner.from_master_seed`, which derives the sr25519 sub-key the
    same way (substrate-interface `Keypair.create_from_seed`) AND the
    ML-DSA-44 sub-key via FIPS 204 `ML_DSA.KeyGen(seed)` — matching the
    Rust `sr25519_mldsa44::Pair::from_string(mnemonic)` derivation used in
    `quip-protocol-rs/crates/transaction-crypto/examples/derive_genesis_keys.rs`.
    """
    from substrateinterface import Keypair, KeypairType

    phrase = path.read_text().strip()
    if not phrase:
        raise ValueError(f"--mnemonic-file is empty: {path}")
    kp = Keypair.create_from_mnemonic(phrase, crypto_type=KeypairType.SR25519)
    seed = kp.seed_hex if isinstance(kp.seed_hex, (bytes, bytearray)) else bytes.fromhex(
        kp.seed_hex.removeprefix("0x")
    )
    if len(seed) != 32:
        raise ValueError(
            f"derived seed has unexpected length {len(seed)} (expected 32) — "
            "check the mnemonic file content"
        )
    return HybridSigner.from_master_seed(seed)


def _build_signer(args: argparse.Namespace) -> HybridSigner:
    if args.mnemonic_file is not None:
        return _signer_from_mnemonic_file(args.mnemonic_file)
    return _signer_from_hex(args.sudo_key)


async def main(args: argparse.Namespace) -> int:
    signer = _build_signer(args)
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
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--sudo-key",
        help="Sudo signer: dev URI (//Alice, //Bob) or 32-byte master seed as hex",
    )
    group.add_argument(
        "--mnemonic-file",
        type=Path,
        help="Path to a BIP39 mnemonic file (e.g. an operator's quip-testnet-keys/operator-N/mnemonic). "
        "Mount the file read-only into the container.",
    )
    parser.add_argument(
        "--validator",
        default="ws://quip-validator:9944",
        help="Substrate validator WebSocket URL (default: %(default)s)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse_args())))
