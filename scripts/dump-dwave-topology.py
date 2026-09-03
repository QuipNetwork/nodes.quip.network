"""Dump the live D-Wave working graph as a seed-chain topology spec.

The chain's registered topology must match the qubits and couplers the QPU
actually has. D-Wave calibrates qubits in and out over time, so a spec built
months ago drifts from the hardware, and every drifted qubit becomes a defect
the miner has to route around.

This reads the graph from the miner's own sampler
(`quip_miner_dwave.ocean.OceanSampler`), so it sees exactly what the miner
sees, including the same credential resolution. It only reads solver
properties. It submits no problem and spends no QPU access time.

Run it on a host with D-Wave credentials:

    docker compose --profile cpu run --rm --no-deps \
      -v "$PWD/scripts/dump-dwave-topology.py:/dump.py:ro" \
      -v "$PWD/config:/out" \
      --entrypoint python3 cpu /dump.py /out/advantage2-system1-h0.spec.json

Credentials and solver selection come from the environment the container
already has (`DWAVE_API_TOKEN`, `DWAVE_API_SOLVER`, `DWAVE_API_REGION`) or
from ~/.config/dwave/dwave.conf. Set `DWAVE_API_SOLVER` when the account can
see more than one solver, or the SDK picks its default.

There is no mock mode: the offline sampler has no hardware graph to report,
so this script only means anything on a host with QPU access.

The printed node and edge counts and the solver chip id are what to check
before seeding. `quip-coordinator seed-chain` computes the authoritative
topology hash when it registers the spec.
"""

import argparse
import json
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", help="Path to write the topology spec JSON.")
    parser.add_argument(
        "--allowed-h-milli",
        default="0",
        help="Comma-separated allowed h field values, in milli units "
        "(default: 0, the h0 topology the chain runs).",
    )
    parser.add_argument(
        "--allowed-j-milli",
        default="-1000,1000",
        help="Comma-separated allowed j coupling values, in milli units "
        "(default: -1000,1000).",
    )
    args = parser.parse_args()

    def milli(text):
        return sorted({int(part) for part in text.split(",") if part.strip()})

    from quip_miner_dwave.ocean import OceanSampler

    sampler = OceanSampler(
        solver_name=os.environ.get("DWAVE_API_SOLVER") or None,
        region=os.environ.get("DWAVE_API_REGION") or None,
        mock=False,
    )
    sampler.ensure_connected()

    nodes = sorted(int(n) for n in sampler.live_nodes)
    edges = sorted(
        (min(int(u), int(v)), max(int(u), int(v))) for u, v in sampler.live_edges
    )
    if not nodes or not edges:
        sys.stderr.write("error: sampler reported an empty graph.\n")
        return 1

    spec = {
        "nodes": nodes,
        "edges": [[u, v] for u, v in edges],
        "allowed_h_milli": milli(args.allowed_h_milli),
        "allowed_j_milli": milli(args.allowed_j_milli),
    }
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(spec, handle, separators=(",", ":"))

    solver = getattr(sampler, "_qpu_solver", None)
    chip = "?"
    if solver is not None:
        chip = solver.properties.get("chip_id", "?")
    native = sampler.native_topology_hash
    print(f"solver chip_id : {chip}")
    print(f"nodes          : {len(nodes)}")
    print(f"edges          : {len(edges)}")
    print(f"allowed_h_milli: {spec['allowed_h_milli']}")
    print(f"allowed_j_milli: {spec['allowed_j_milli']}")
    print(f"native hash    : {native.hex() if native else '(none)'}")
    print(f"wrote          : {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
