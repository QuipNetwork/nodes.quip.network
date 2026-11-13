#!/usr/bin/env python3
"""Wait until the chain has mined its first qblock, or fail.

`make localdev` runs this after the miner starts. One qblock proves the whole
dev stack works: the seeded topology is the default, the miner funded and
registered itself, generated a family instance, and the runtime accepted the
proof. The script polls `QuantumPow.QBlockCount` through the substrate RPC
that Caddy serves at /rpc.

Usage: wait-qblock.py [RPC_URL] [TIMEOUT_SECONDS]
Defaults: http://localhost:20049/rpc and 600 seconds.

Stdlib only, like the other scripts here.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

# twox128("QuantumPow") ++ twox128("QBlockCount"). The value is a SCALE u64.
QBLOCK_COUNT_KEY = "0x9b2c4dbe49d7a1aed7ce99e4b8c072e8a917cf9ea4fd296f6933d75be837c3e1"
POLL_SECONDS = 5


def qblock_count(url: str) -> int:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "state_getStorage", "params": [QBLOCK_COUNT_KEY]}
    ).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        result = json.load(response).get("result")
    # An unset ValueQuery is not stored: no qblock yet.
    return int.from_bytes(bytes.fromhex(result[2:]), "little") if result else 0


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:20049/rpc"
    timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 600
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            count = qblock_count(url)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last_error = str(exc)
        else:
            if count > 0:
                print(f"qblock {count} mined")
                return 0
        time.sleep(POLL_SECONDS)
    detail = f" (last RPC error: {last_error})" if last_error else ""
    print(f"no qblock within {timeout} s at {url}{detail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
