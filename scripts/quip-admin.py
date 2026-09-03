"""Submit root-origin QuantumPow calls to a live Quip chain.

`quip-coordinator seed-chain` bootstraps a fresh chain and refuses to run
twice, so it cannot repoint a network that is already seeded. This tool covers
the operator path the pallet documents for `set_default_topology`:

    "register_topology only seeds DefaultTopology on the very first
     registration; this call is the operator path for upgrading a live chain
     to a new topology (e.g. tracking a QPU's working graph across
     calibrations)."

Sudo on a Quip chain is an H4 hybrid account (sr25519 + FN-DSA-512), which no
general-purpose Substrate library can sign for. Signing therefore comes from
the chain's own `quip_signer` binding, built from
`quip-validator/crates/transaction-crypto-py`. Everything else here mirrors
`quip-coordinator/src/chain/extrinsic.rs` and `chain/seed.rs`.

Requires `quip_signer` and `xxhash`. Build and run in a venv:

    git clone --depth 1 https://gitlab.com/quip.network/quip-validator.git
    (cd quip-validator/crates/transaction-crypto-py && maturin build --release --out /tmp/w)
    uv run --with xxhash --with /tmp/w/quip_signer-*.whl \
        python scripts/quip-admin.py --help

Run `self-test` first: it checks the topology hash against the golden fixture
pinned in the coordinator's own test suite. A mismatch means this script and
the chain disagree, and nothing should be submitted.

Every submitting subcommand reads the storage entry its own success writes and
reports what the chain actually holds afterwards.
"""

import argparse
import hashlib
import json
import sys
import time
import urllib.request

# Runtime construct indices, from quip-coordinator/src/chain/scale_types.rs
# and pallets/quantum-pow/src/lib.rs `#[pallet::call_index(..)]`.
SUDO_PALLET_INDEX = 6
SUDO_CALL_INDEX = 0
QUANTUM_POW_PALLET_INDEX = 10
CALL_REGISTER_TOPOLOGY = 2
CALL_SET_DIFFICULTY = 3
CALL_SET_DEFAULT_TOPOLOGY = 5
CALL_ADD_MINEABLE_TOPOLOGY = 6
CALL_REMOVE_MINEABLE_TOPOLOGY = 7

SS58_PREFIX = 42

# Pinned in quip-coordinator/src/topology.rs `golden_topology_hash_known_fixture`.
GOLDEN = {
    "nodes": [0, 1, 2],
    "edges": [(0, 1), (1, 2)],
    "h": [-1000, 0, 1000],
    "j": [-1000, 1000],
    "spin": [-1000, 1000],
    "hash": "56dff5ca824517ba7f0593ec12a5dd102eb0a14f775b35c415467e0e0d6e19c2",
}


# --------------------------------------------------------------- SCALE ----
def compact(n):
    """SCALE compact integer."""
    if n < 0:
        raise ValueError("compact is unsigned")
    if n < 64:
        return bytes([n << 2])
    if n < 1 << 14:
        return ((n << 2) | 1).to_bytes(2, "little")
    if n < 1 << 30:
        return ((n << 2) | 2).to_bytes(4, "little")
    body = n.to_bytes((n.bit_length() + 7) // 8, "little")
    return bytes([((len(body) - 4) << 2) | 3]) + body


def vec_u32(values):
    return compact(len(values)) + b"".join(int(v).to_bytes(4, "little") for v in values)


def vec_edges(edges):
    out = compact(len(edges))
    for u, v in edges:
        out += int(u).to_bytes(4, "little") + int(v).to_bytes(4, "little")
    return out


def vec_bytes(raw):
    return compact(len(raw)) + raw


def canonical_bytes_set(values):
    """`AllowedValueSpec::Set(..).canonical_bytes()`: tag 0, then sorted BE i32."""
    out = bytes([0])
    for value in sorted(int(v) for v in values):
        out += int(value).to_bytes(4, "big", signed=True)
    return out


def blake2_256(data):
    return hashlib.blake2b(data, digest_size=32).digest()


def topology_hash(nodes, edges, allowed_h, allowed_j, allowed_spin):
    """Byte-identical to `pallet_quantum_pow::topology::hash_topology`.

    Nodes sort ascending, each edge is stored low-endpoint first and the edge
    list sorts, and each allowed-value spec contributes its canonical bytes as
    a SCALE `Vec<u8>` (so it carries a compact length prefix).
    """
    canon_nodes = sorted(int(n) for n in nodes)
    canon_edges = sorted((min(int(u), int(v)), max(int(u), int(v))) for u, v in edges)
    payload = (
        vec_u32(canon_nodes)
        + vec_edges(canon_edges)
        + vec_bytes(canonical_bytes_set(allowed_h))
        + vec_bytes(canonical_bytes_set(allowed_j))
        + vec_bytes(canonical_bytes_set(allowed_spin))
    )
    return blake2_256(payload)


# ---------------------------------------------------------- call bodies ----
def _sudo(inner_call_index, args=b""):
    return (
        bytes([SUDO_PALLET_INDEX, SUDO_CALL_INDEX,
               QUANTUM_POW_PALLET_INDEX, inner_call_index])
        + args
    )


def encode_register_topology(nodes, edges, allowed_h, allowed_j, allowed_spin):
    args = (
        vec_u32(sorted(int(n) for n in nodes))
        + vec_edges(sorted((min(u, v), max(u, v)) for u, v in edges))
        + bytes([0]) + vec_u32_i32(allowed_h)
        + bytes([0]) + vec_u32_i32(allowed_j)
        + bytes([0]) + vec_u32_i32(allowed_spin)
    )
    return _sudo(CALL_REGISTER_TOPOLOGY, args)


def vec_u32_i32(values):
    """SCALE `Vec<i32>` — the payload of `AllowedValueSpec::Set`."""
    vs = sorted(int(v) for v in values)
    return compact(len(vs)) + b"".join(v.to_bytes(4, "little", signed=True) for v in vs)


def encode_set_default_topology(topology_hash_bytes):
    return _sudo(CALL_SET_DEFAULT_TOPOLOGY, topology_hash_bytes)


def encode_add_mineable_topology(topology_hash_bytes):
    return _sudo(CALL_ADD_MINEABLE_TOPOLOGY, topology_hash_bytes)


def encode_remove_mineable_topology(topology_hash_bytes):
    return _sudo(CALL_REMOVE_MINEABLE_TOPOLOGY, topology_hash_bytes)


def encode_set_difficulty(topology_hash_bytes, min_solutions, max_energy_milli,
                          min_diversity_milli):
    """`DifficultyConfig` is `u32, i64, u32` in that order."""
    args = (
        topology_hash_bytes
        + int(min_solutions).to_bytes(4, "little")
        + int(max_energy_milli).to_bytes(8, "little", signed=True)
        + int(min_diversity_milli).to_bytes(4, "little")
    )
    return _sudo(CALL_SET_DIFFICULTY, args)


# ------------------------------------------------------------- identity ----
def ss58_encode(account_id, prefix=SS58_PREFIX):
    import base58

    payload = bytes([prefix]) + account_id
    checksum = hashlib.blake2b(b"SS58PRE" + payload, digest_size=64).digest()[:2]
    return base58.b58encode(payload + checksum).decode()


def twox128(data):
    import xxhash

    return b"".join(
        xxhash.xxh64(data, seed=s).intdigest().to_bytes(8, "little") for s in (0, 1)
    )


def storage_key(pallet, item, map_key=None):
    """Plain storage key, or a `blake2_128concat` map key when `map_key` is given."""
    key = twox128(pallet.encode()) + twox128(item.encode())
    if map_key is not None:
        key += hashlib.blake2b(map_key, digest_size=16).digest() + map_key
    return "0x" + key.hex()


# ------------------------------------------------------------------ RPC ----
class Rpc:
    def __init__(self, url):
        self.url = url
        self._id = 0

    def call(self, method, params):
        self._id += 1
        body = json.dumps(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        ).encode()
        request = urllib.request.Request(
            self.url, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
        if "error" in payload:
            raise RuntimeError(f"{method}: {payload['error']}")
        return payload.get("result")


def signed_extension_context(rpc, account_id):
    """Nonce, genesis hash, and runtime versions, as `real.rs` gathers them."""
    nonce = rpc.call("system_accountNextIndex", [ss58_encode(account_id)])
    genesis = bytes.fromhex(rpc.call("chain_getBlockHash", [0])[2:])
    version = rpc.call("state_getRuntimeVersion", [])
    return {
        "nonce": int(nonce),
        "genesis": genesis,
        "spec_version": int(version["specVersion"]),
        "transaction_version": int(version["transactionVersion"]),
        "tip": 0,
    }


def build_signed_extrinsic(signer, call_bytes, ctx):
    """Mirrors `build_hybrid_signed_extrinsic` in chain/extrinsic.rs."""
    extra = (
        bytes([0x00])                    # CheckMortality: Era::Immortal
        + compact(ctx["nonce"])          # CheckNonce
        + compact(ctx["tip"])            # ChargeTransactionPayment
        + bytes([0x00])                  # CheckMetadataHash: Disabled
    )
    additional = (
        ctx["spec_version"].to_bytes(4, "little")
        + ctx["transaction_version"].to_bytes(4, "little")
        + ctx["genesis"]                 # CheckGenesis
        + ctx["genesis"]                 # CheckMortality, immortal -> genesis
        + bytes([0x00])                  # CheckMetadataHash: Option::None
    )
    payload = call_bytes + extra + additional
    # Substrate's SignedPayload rule: hash anything longer than 256 bytes.
    to_sign = blake2_256(payload) if len(payload) > 256 else payload
    envelope = signer.sign(to_sign)      # SCALE envelope: public || signature

    body = (
        bytes([0x84])                    # v4, signed
        + bytes([0x00])                  # MultiAddress::Id
        + signer.account_id
        + envelope
        + extra
        + call_bytes
    )
    return compact(len(body)) + body


def submit(rpc, signer, call_bytes, dry_run):
    ctx = signed_extension_context(rpc, signer.account_id)
    extrinsic = build_signed_extrinsic(signer, call_bytes, ctx)
    print(f"  nonce={ctx['nonce']} spec={ctx['spec_version']} "
          f"tx={ctx['transaction_version']} bytes={len(extrinsic)}")
    if dry_run:
        print("  dry run: not submitted")
        return None
    tx_hash = rpc.call("author_submitExtrinsic", ["0x" + extrinsic.hex()])
    print(f"  submitted: {tx_hash}")
    return tx_hash


# ------------------------------------------------------------ chain read ----
def read_default_topology(rpc):
    value = rpc.call("state_getStorage", [storage_key("QuantumPow", "DefaultTopology")])
    return value[2:] if value else None


def is_registered(rpc, topology_hash_bytes):
    return rpc.call("state_getStorage",
                    [storage_key("QuantumPow", "RegisteredTopologies",
                                 topology_hash_bytes)]) is not None


def is_mineable(rpc, topology_hash_bytes):
    return rpc.call("state_getStorage",
                    [storage_key("QuantumPow", "MineableTopologies",
                                 topology_hash_bytes)]) is not None


def await_state(check, what, attempts=20, delay=3):
    """Poll chain state until `check` holds. Inclusion is not success."""
    for _ in range(attempts):
        if check():
            print(f"  confirmed: {what}")
            return True
        time.sleep(delay)
    print(f"  NOT confirmed after {attempts * delay}s: {what}", file=sys.stderr)
    return False


# ------------------------------------------------------------------ spec ----
def load_spec(path):
    with open(path, encoding="utf-8") as handle:
        spec = json.load(handle)
    nodes = spec["nodes"]
    edges = [tuple(e) for e in spec["edges"]]
    allowed_h = spec["allowed_h_milli"]
    allowed_j = spec["allowed_j_milli"]
    # The seed path's default when a spec omits it; matches what the live
    # chain stores for the registered Advantage2 topology.
    allowed_spin = spec.get("allowed_spin_milli", [-1000, 1000])
    return nodes, edges, allowed_h, allowed_j, allowed_spin


def resolve_hash(args):
    """Topology hash from --hash, or computed from --topology."""
    if getattr(args, "hash", None):
        raw = bytes.fromhex(args.hash[2:] if args.hash.startswith("0x") else args.hash)
        if len(raw) != 32:
            raise SystemExit("error: --hash must be 32 bytes")
        return raw
    if getattr(args, "topology", None):
        return topology_hash(*load_spec(args.topology))
    raise SystemExit("error: pass --hash or --topology")


def load_signer(path):
    from quip_signer import HybridSigner

    with open(path, encoding="utf-8") as handle:
        material = handle.read().strip()
    return HybridSigner.from_mnemonic(material)


# ------------------------------------------------------------------ main ----
def cmd_self_test(args):
    got = topology_hash(GOLDEN["nodes"], GOLDEN["edges"],
                        GOLDEN["h"], GOLDEN["j"], GOLDEN["spin"]).hex()
    ok = got == GOLDEN["hash"]
    print(f"golden fixture: {'PASS' if ok else 'FAIL'}")
    print(f"  expected {GOLDEN['hash']}")
    print(f"  got      {got}")
    if args.topology:
        print(f"spec hash: 0x{topology_hash(*load_spec(args.topology)).hex()}")
    return 0 if ok else 1


def cmd_topology_hash(args):
    print("0x" + topology_hash(*load_spec(args.topology)).hex())
    return 0


def cmd_show(args):
    rpc = Rpc(args.rpc)
    current = read_default_topology(rpc)
    print(f"DefaultTopology: 0x{current}" if current else "DefaultTopology: (unset)")
    if args.topology or args.hash:
        h = resolve_hash(args)
        print(f"queried hash   : 0x{h.hex()}")
        print(f"  registered   : {is_registered(rpc, h)}")
        print(f"  mineable     : {is_mineable(rpc, h)}")
        print(f"  is default   : {current == h.hex()}")
    return 0


def _submit_and_confirm(args, call_bytes, check, what):
    rpc = Rpc(args.rpc)
    signer = load_signer(args.mnemonic_file)
    print(f"signer account : 0x{bytes(signer.account_id).hex()}")
    print(f"                 {ss58_encode(bytes(signer.account_id))}")
    submit(rpc, signer, call_bytes, args.dry_run)
    if args.dry_run:
        return 0
    return 0 if await_state(check, what) else 1


def cmd_register_topology(args):
    nodes, edges, h, j, spin = load_spec(args.topology)
    digest = topology_hash(nodes, edges, h, j, spin)
    print(f"topology       : {len(nodes)} nodes, {len(edges)} edges")
    print(f"topology hash  : 0x{digest.hex()}")
    rpc = Rpc(args.rpc)
    if is_registered(rpc, digest):
        print("  already registered; nothing to do")
        return 0
    call = encode_register_topology(nodes, edges, h, j, spin)
    return _submit_and_confirm(args, call,
                               lambda: is_registered(Rpc(args.rpc), digest),
                               "topology registered")


def cmd_set_default(args):
    digest = resolve_hash(args)
    rpc = Rpc(args.rpc)
    print(f"topology hash  : 0x{digest.hex()}")
    if not is_registered(rpc, digest):
        raise SystemExit("error: topology is not registered; run register-topology first")
    if not is_mineable(rpc, digest):
        raise SystemExit("error: topology is not mineable; run add-mineable-topology first")
    return _submit_and_confirm(args, encode_set_default_topology(digest),
                               lambda: read_default_topology(Rpc(args.rpc)) == digest.hex(),
                               "DefaultTopology repointed")


def cmd_add_mineable(args):
    digest = resolve_hash(args)
    print(f"topology hash  : 0x{digest.hex()}")
    return _submit_and_confirm(args, encode_add_mineable_topology(digest),
                               lambda: is_mineable(Rpc(args.rpc), digest),
                               "topology is mineable")


def cmd_remove_mineable(args):
    digest = resolve_hash(args)
    print(f"topology hash  : 0x{digest.hex()}")
    return _submit_and_confirm(args, encode_remove_mineable_topology(digest),
                               lambda: not is_mineable(Rpc(args.rpc), digest),
                               "topology is no longer mineable")


def cmd_set_difficulty(args):
    digest = resolve_hash(args)
    print(f"topology hash  : 0x{digest.hex()}")
    print(f"difficulty     : min_solutions={args.min_solutions} "
          f"max_energy_milli={args.max_energy_milli} "
          f"min_diversity_milli={args.min_diversity_milli}")
    call = encode_set_difficulty(digest, args.min_solutions,
                                 args.max_energy_milli, args.min_diversity_milli)
    key = storage_key("QuantumPow", "Difficulties", digest)
    return _submit_and_confirm(
        args, call,
        lambda: Rpc(args.rpc).call("state_getStorage", [key]) is not None,
        "difficulty written")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def chain_args(p, need_key=True):
        p.add_argument("--rpc", default="http://127.0.0.1:9944",
                       help="Validator JSON-RPC endpoint.")
        if need_key:
            p.add_argument("--mnemonic-file", required=True,
                           help="File holding the sudo BIP39 mnemonic. Mount read-only.")
            p.add_argument("--dry-run", action="store_true",
                           help="Build and print the extrinsic without submitting.")

    def target_args(p):
        p.add_argument("--hash", help="Topology hash, 32-byte hex.")
        p.add_argument("--topology", help="Topology spec JSON; the hash is computed from it.")

    p = sub.add_parser("self-test", help="Check the hash against the pinned golden fixture.")
    p.add_argument("--topology", help="Also print this spec's hash.")
    p.set_defaults(func=cmd_self_test)

    p = sub.add_parser("topology-hash", help="Print a spec's chain topology hash. Offline.")
    p.add_argument("--topology", required=True)
    p.set_defaults(func=cmd_topology_hash)

    p = sub.add_parser("show", help="Read DefaultTopology and a topology's status.")
    chain_args(p, need_key=False)
    target_args(p)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("register-topology", help="sudo QuantumPow.register_topology")
    chain_args(p)
    p.add_argument("--topology", required=True)
    p.set_defaults(func=cmd_register_topology)

    p = sub.add_parser("add-mineable-topology", help="sudo QuantumPow.add_mineable_topology")
    chain_args(p)
    target_args(p)
    p.set_defaults(func=cmd_add_mineable)

    p = sub.add_parser("remove-mineable-topology",
                       help="sudo QuantumPow.remove_mineable_topology")
    chain_args(p)
    target_args(p)
    p.set_defaults(func=cmd_remove_mineable)

    p = sub.add_parser("set-default-topology", help="sudo QuantumPow.set_default_topology")
    chain_args(p)
    target_args(p)
    p.set_defaults(func=cmd_set_default)

    p = sub.add_parser("set-difficulty", help="sudo QuantumPow.set_difficulty")
    chain_args(p)
    target_args(p)
    p.add_argument("--min-solutions", type=int, required=True)
    p.add_argument("--max-energy-milli", type=int, required=True)
    p.add_argument("--min-diversity-milli", type=int, required=True)
    p.set_defaults(func=cmd_set_difficulty)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
