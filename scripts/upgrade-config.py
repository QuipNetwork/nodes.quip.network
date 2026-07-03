"""Upgrade a v0.1 quip-node data/ directory to the v0.2 quip-miner schema.

v0.1 ships a monolithic `[global]` table plus backend tables (`[cpu]`,
`[gpu]`, `[cuda.N]`, `[qpu]`, `[dwave]`, ...). v0.2 renames `[global]` to
`[miner]`, drops every P2P/transport key (substrate validator owns p2p
now), promotes `public_host`/`public_port`/`log_level`/`node_log` into
`[miner]`, and keeps the backend tables verbatim.

Stdlib-only (Python 3.11+ tomllib for read, hand-rolled writer for emit)
so this runs anywhere the operator already has python3 — and on Python
3.10 hosts via `make upgrade-config-docker`.

Behavior:
  - Detects v0.1 by presence of [global], v0.2 by [miner]. Refuses to run
    on ambiguous (both) or unrecognized (neither) configs.
  - On v0.1 detection, moves every entry in DATA_DIR (except an existing
    .v0.1_backup itself) into DATA_DIR/.v0.1_backup/, then writes a fresh
    DATA_DIR/config.toml in v0.2 shape using values harvested from the
    backed-up file.
  - Refuses to clobber an existing .v0.1_backup/ — that signals the dir
    has already been migrated.
"""

import argparse
import os
import re
import shutil
import sys
import tomllib
from pathlib import Path

# .env keys removed in v0.2 (operator's stale .env carries them forward;
# the dashboard image used to read them, the v0.2 image consumes
# QUIP_VALIDATOR_RPC_URLS instead). Match both commented and uncommented
# forms so we strip leftover documentation lines too.
ENV_DROP_KEY_PATTERN = re.compile(
    r"^\s*#?\s*(QUIP_NODE_URL|QUIP_NODE_TOKEN)\s*=", re.IGNORECASE
)
ENV_BACKUP_NAME = ".env.v0.1_backup"

# Block appended to .env when QUIP_VALIDATOR_RPC_URLS isn't already documented.
ENV_V02_BLOCK = """
# --- v0.2 dashboard indexer (added by scripts/upgrade-config.py) ------------
# Drives both the chain-side indexer (substrate WS, epochs/finalized/etc.)
# and the miner REST surface that Caddy fronts on the same host. Comma-
# separated; defaults to the colocated validator (ws://quip-validator:9944)
# when unset, so most operators don't need to set this explicitly. Override
# only when the miner runs on a host without its own local validator.
# QUIP_VALIDATOR_RPC_URLS=ws://quip-validator:9944
"""

BACKUP_DIRNAME = ".v0.1_backup"

# Backend tables preserved verbatim from v0.1 → v0.2 (semantics + key
# inheritance unchanged in the v0.2 loader).
PRESERVED_BACKEND_TABLES = frozenset({
    "cpu", "gpu", "cuda", "nvidia", "metal", "modal",
    "qpu", "dwave", "ibm", "braket", "pasqal", "ionq", "origin",
})

# v0.1 [global] keys that map directly into v0.2 [miner].
# rest_port is intentionally NOT in this list — see CADDY_PROXY_REST_PORT below.
PROMOTED_GLOBAL_KEYS = (
    "node_name",
    "public_host",
    "public_port",
    "rest_host",
    "log_level",
    "node_log",
)

# v0.2 Caddy fronts the miner's REST API and proxies /api/v1/* to
# quip-miner:8086 (the upstream image's rest_port default — see
# caddy/Caddyfile). The miner's telemetry process MUST bind this port
# for the dashboard indexer + dashboard UI to reach it. v0.1 deployments
# commonly used 443 (miner-terminated TLS) or other ports, so we force
# the v0.2 convention regardless of what the v0.1 config said, and emit
# a warning when overriding.
CADDY_PROXY_REST_PORT = 8086

# Canonical testnet faucet, written into configs that lack faucet_url so
# the miner's first-boot self-bootstrap (register + fund) keeps working
# now that the QUIP_FAUCET_URL env var is gone (the v0.2.1-rc miner
# images have no configuration env vars).
FAUCET_TESTNET_URL = "https://faucet.testnet.quip.network"

# Miner env vars with no consumer as of the quip-protocol v0.2.1-rc
# images — the miner is fully config-driven. Uncommented values are
# harvested into config.toml before the lines are stripped from .env.
DEAD_MINER_ENV_KEYS = (
    "QUIP_VALIDATORS",
    "QUIP_FAUCET_URL",
    "QUIP_REST_PORT",
    "QUIP_REST_HOST",
    "QUIP_SIGNER_KEY",
    "QUIP_MODE",
)
DEAD_MINER_ENV_PATTERN = re.compile(
    r"^\s*(#\s*)?(" + "|".join(DEAD_MINER_ENV_KEYS) + r")\s*=\s*(.*)$"
)
ENV_BACKUP_NAME_V02 = ".env.pre-config-driven_backup"
CONFIG_BACKFILL_BACKUP = "config.toml.pre-backfill.bak"

# v0.1 [global] keys we drop SILENTLY (no operator action needed; backup
# retains the original value).
SILENT_DROP_GLOBAL_KEYS = frozenset({
    "secret", "genesis_config", "auto_mine", "peer",
    "timeout", "heartbeat_interval", "heartbeat_timeout", "fanout",
    "verify_tls", "ca_bundle",
    "tls_cert_file", "tls_key_file",
    "rest_tls_cert_file", "rest_tls_key_file",
    "tofu", "trust_db",
    "rest_insecure_port", "webroot", "http_log",
    "telemetry_enabled", "telemetry_dir",
})

# v0.1 [global] keys we drop with a LOUD warning. v0.2 silently aliases
# listen → rest_host and port → rest_port in the loader, but the
# semantics flipped (QUIC peer → telemetry REST) so an operator with
# port = 20049 would unintentionally expose the REST API on what used to
# be a peer port. We refuse to alias and surface the choice instead.
LOUD_DROP_GLOBAL_KEYS = frozenset({"listen", "port"})

# Top-level v0.1 tables we drop entirely.
DROPPED_TOP_TABLES = frozenset({"telemetry_api"})


def _emit_string(s):
    if any(c in s for c in '\n\r\t\\"'):
        escaped = (s.replace("\\", "\\\\")
                    .replace('"', '\\"')
                    .replace("\n", "\\n")
                    .replace("\r", "\\r")
                    .replace("\t", "\\t"))
        return f'"{escaped}"'
    return f'"{s}"'


def _emit_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return _emit_string(v)
    if isinstance(v, list):
        return "[" + ", ".join(_emit_value(x) for x in v) + "]"
    raise TypeError(f"unsupported TOML value type: {type(v).__name__}")


def _emit_table(prefix, table, lines):
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
    subtables = {k: v for k, v in table.items() if isinstance(v, dict)}
    # Emit header when there are scalars OR when this is a leaf marker
    # (e.g. an empty [qpu] section signaling QPU-miner presence).
    if scalars or not subtables:
        lines.append(f"[{prefix}]")
        for k, v in scalars.items():
            lines.append(f"{k} = {_emit_value(v)}")
        lines.append("")
    for k, v in subtables.items():
        _emit_table(f"{prefix}.{k}", v, lines)


def _render_miner_section(harvested):
    """Render the [miner] table from harvested v0.1 [global] values."""
    out = []
    out.append("# quip-miner v0.2 [miner] schema, written by scripts/upgrade-config.py.")
    out.append("# See data/config.toml in the nodes.quip.network repo for the canonical")
    out.append("# template with inline documentation for every key.")
    out.append("")
    out.append("[miner]")

    validators = harvested.get("validators") or ["ws://quip-validator:9944"]
    out.append("validators = [")
    for url in validators:
        out.append(f"    {_emit_string(url)},")
    out.append("]")

    out.append(f'signer_key = {_emit_string(harvested.get("signer_key", "/data/keystore.json"))}')
    out.append(f"faucet_url = {_emit_string(FAUCET_TESTNET_URL)}")

    # rest_port is forced to the v0.2 Caddy-proxy convention regardless of
    # the v0.1 value; see _render_config for the warning emitted when this
    # overrides a mismatched v0.1 setting.
    out.append(f"rest_port = {CADDY_PROXY_REST_PORT}")
    out.append(f'rest_host = {_emit_string(harvested.get("rest_host", "0.0.0.0"))}')

    for key in ("node_name", "public_host", "public_port", "log_level", "node_log"):
        if key in harvested:
            out.append(f"{key} = {_emit_value(harvested[key])}")

    out.append("")
    return out


def _render_config(parsed, warnings):
    """Build the new config.toml content as a string.

    Mutates `warnings` (list[str]) with operator-actionable notes that
    the caller prints to stderr.
    """
    global_table = parsed.get("global", {})

    harvested = {}
    for key in PROMOTED_GLOBAL_KEYS:
        if key in global_table:
            harvested[key] = global_table[key]

    for key in LOUD_DROP_GLOBAL_KEYS:
        if key in global_table:
            warnings.append(
                f"dropping [global].{key}={global_table[key]!r}: v0.2 aliases this to "
                f"rest_{'host' if key == 'listen' else 'port'} but semantics changed "
                f"(was QUIC peer, now telemetry REST). Set [miner].rest_{'host' if key == 'listen' else 'port'} "
                f"explicitly if you want the REST API on that interface."
            )

    if "auto_mine" in global_table and global_table["auto_mine"] is False:
        warnings.append(
            "[global].auto_mine=false in v0.1 disabled mining until peers connected; "
            "v0.2 miners mine unconditionally once connected to a validator."
        )

    if "peer" in global_table:
        warnings.append(
            f"[global].peer had {len(global_table['peer'])} entries — v0.2 has no P2P "
            "mesh. Set [miner].validators to your substrate validator WS URL(s)."
        )

    if "rest_port" in global_table and global_table["rest_port"] != CADDY_PROXY_REST_PORT:
        warnings.append(
            f"forcing [miner].rest_port to {CADDY_PROXY_REST_PORT} (v0.2 Caddy proxies "
            f"/api/v1/* to quip-miner:{CADDY_PROXY_REST_PORT}); your v0.1 [global]."
            f"rest_port={global_table['rest_port']!r} was dropped because the miner no "
            "longer terminates TLS itself — Caddy does. If you genuinely need a "
            "different internal port, edit [miner].rest_port and caddy/Caddyfile "
            "together."
        )

    # Surface unknown [global] keys so we don't silently lose operator-tuned
    # values we haven't catalogued.
    known = (set(PROMOTED_GLOBAL_KEYS)
             | {"rest_port"}
             | SILENT_DROP_GLOBAL_KEYS
             | LOUD_DROP_GLOBAL_KEYS)
    for key in global_table:
        if key not in known:
            warnings.append(
                f"unknown [global].{key} dropped (preserved in backup); review backup "
                "if this was operator-tuned."
            )

    lines = _render_miner_section(harvested)

    for table_name, table in parsed.items():
        if table_name == "global":
            continue
        if table_name in DROPPED_TOP_TABLES:
            warnings.append(
                f"dropping [{table_name}] table — not in v0.2 schema (deployment-layer "
                "concern now)."
            )
            continue
        if table_name not in PRESERVED_BACKEND_TABLES:
            warnings.append(
                f"unknown top-level table [{table_name}] dropped (preserved in backup)."
            )
            continue
        if table_name == "dwave" and "token" in table:
            warnings.append(
                "[dwave].token preserved verbatim. v0.2 convention prefers DWAVE_API_KEY "
                "in the environment — consider moving the secret out of config.toml."
            )
        _emit_table(table_name, table, lines)

    return "\n".join(lines).rstrip() + "\n"


def _detect_schema(parsed, path):
    has_global = "global" in parsed
    has_miner = "miner" in parsed
    if has_global and has_miner:
        sys.stderr.write(
            f"error: {path} has both [global] and [miner]; ambiguous schema. "
            "Manual review required.\n"
        )
        sys.exit(2)
    if not has_global and not has_miner:
        sys.stderr.write(
            f"error: {path} has neither [global] nor [miner]; not a recognizable "
            "quip miner/node config.\n"
        )
        sys.exit(2)
    return "v0.1" if has_global else "v0.2"


def _preflight_writable(data_dir):
    """Verify the calling user can mkdir + move within data_dir.

    Catches the common v0.1 deployment case where data/ is root-owned (from a
    container that ran as root pre-PUID), but the operator now runs the
    converter under their shell user. Without this check `shutil.move` raises
    a PermissionError mid-loop, potentially leaving data/ half-backed-up.
    """
    if not os.access(data_dir, os.W_OK | os.X_OK):
        sys.stderr.write(
            f"error: {data_dir} is not writable by uid={os.getuid()}.\n"
            f"  The converter needs to create {data_dir}/{BACKUP_DIRNAME}/ and\n"
            f"  move every existing entry into it. Fix ownership first:\n"
            f"    sudo chown -R \"$(id -u):$(id -g)\" {data_dir}\n"
            f"  Then re-run.\n"
        )
        sys.exit(1)


def _backup(data_dir, dry_run):
    backup = data_dir / BACKUP_DIRNAME
    if backup.exists():
        sys.stderr.write(
            f"error: {backup} already exists; this dir looks already-migrated. "
            "Remove or rename it to re-run.\n"
        )
        sys.exit(1)

    if not dry_run:
        _preflight_writable(data_dir)

    entries = [p for p in data_dir.iterdir() if p.name != BACKUP_DIRNAME]
    if dry_run:
        print(f"[dry-run] would create {backup} and move into it:")
        for p in entries:
            print(f"  - {p.name}")
        return backup

    backup.mkdir()
    for p in entries:
        try:
            shutil.move(str(p), str(backup / p.name))
        except PermissionError as exc:
            sys.stderr.write(
                f"error: can't move {p} into {backup}: {exc}\n"
                f"  Fix ownership first:\n"
                f"    sudo chown -R \"$(id -u):$(id -g)\" {data_dir}\n"
                f"  Then `mv {backup}/* {data_dir}/`, rmdir {backup}, and re-run.\n"
            )
            sys.exit(1)
    return backup


def _upgrade_env_file(env_path, dry_run, warnings):
    """Migrate the operator's `.env` from v0.1 to v0.2 var names.

    v0.1 dashboard image read `QUIP_NODE_URL` and `QUIP_NODE_TOKEN`;
    v0.2 uses `QUIP_VALIDATOR_RPC_URLS`. Operators upgrading by
    `git pull`-ing the repo and running `make updateconfig` would
    otherwise carry stale entries forward — the new image would either
    ignore them or be misled by their values (the dashboard image
    auto-derived a public URL from `QUIP_HOSTNAME` when QUIP_NODE_URL
    was unset, but a stale uncommented value pinned it to the wrong
    place).

    Backs up the existing `.env` to `.env.v0.1_backup` alongside it,
    drops every QUIP_NODE_URL / QUIP_NODE_TOKEN line (commented or
    uncommented — we strip leftover docs too), and appends a
    QUIP_VALIDATOR_RPC_URLS comment block when one isn't present.
    """
    if not env_path.is_file():
        return

    backup = env_path.parent / ENV_BACKUP_NAME
    if backup.exists():
        warnings.append(
            f"{backup} already exists; skipping .env migration "
            "(looks already-migrated)."
        )
        return

    content = env_path.read_text()
    new_lines = []
    drops = []
    for line in content.splitlines():
        m = ENV_DROP_KEY_PATTERN.match(line)
        if m:
            drops.append(m.group(1).upper())
            continue
        new_lines.append(line)

    has_v02_var = "QUIP_VALIDATOR_RPC_URLS" in content
    needs_change = bool(drops) or not has_v02_var
    if not needs_change:
        return

    if not has_v02_var:
        new_lines.append(ENV_V02_BLOCK.rstrip("\n"))

    summary_bits = []
    if drops:
        summary_bits.append(f"dropped {sorted(set(drops))}")
    if not has_v02_var:
        summary_bits.append("appended commented QUIP_VALIDATOR_RPC_URLS placeholder")

    if dry_run:
        warnings.append(
            f"[dry-run] would back up {env_path} → {backup}; "
            + "; ".join(summary_bits)
        )
        return

    # Write new content to a tmp file in the same dir, then atomically
    # rename(env → backup) + rename(tmp → env). If either step fails
    # we leave the original file intact rather than half-written.
    tmp = env_path.parent / (env_path.name + ".tmp")
    tmp.write_text("\n".join(new_lines) + "\n")
    env_path.rename(backup)
    tmp.rename(env_path)
    warnings.append(
        f"migrated .env: backed up {env_path} → {backup}; " + "; ".join(summary_bits)
    )


def _miner_bounds(lines):
    """Return (header_idx, end_idx) of the [miner] section, end exclusive."""
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^\s*\[miner\]\s*$", line):
            start = i
            continue
        if start is not None and re.match(r"^\s*\[", line):
            return start, i
    if start is None:
        raise ValueError("config has no [miner] section")
    return start, len(lines)


def _remove_toml_array(lines, start, end, key):
    """Remove `key = [ ... ]` (single- or multi-line) within lines[start:end].

    Returns the number of removed lines (0 when the key wasn't found).
    """
    open_re = re.compile(rf"^\s*{key}\s*=\s*\[")
    for i in range(start, end):
        if open_re.match(lines[i]):
            j = i
            while "]" not in lines[j]:
                j += 1
            del lines[i : j + 1]
            return j + 1 - i
    return 0


def _read_dead_env(env_path):
    """Read .env, harvesting uncommented dead-miner-var values.

    Returns (harvested: dict, kept_lines: list, dropped_keys: set).
    Read-only — `_write_stripped_env` performs the actual strip after the
    config backfill has consumed the harvested values.
    """
    harvested, kept, dropped = {}, [], set()
    if env_path is None or not env_path.is_file():
        return harvested, kept, dropped
    for line in env_path.read_text().splitlines():
        m = DEAD_MINER_ENV_PATTERN.match(line)
        if not m:
            kept.append(line)
            continue
        commented, key, value = m.group(1), m.group(2), m.group(3).strip()
        if not commented and key not in harvested:
            harvested[key] = value
        dropped.add(key)
    return harvested, kept, dropped


def _write_stripped_env(env_path, kept, dropped, dry_run, warnings):
    """Strip dead miner env lines from .env (backup first, atomic swap)."""
    if not dropped:
        return
    backup = env_path.parent / ENV_BACKUP_NAME_V02
    if backup.exists():
        warnings.append(
            f"{backup} already exists; leaving {env_path} untouched "
            "(looks already-migrated)."
        )
        return
    if dry_run:
        warnings.append(
            f"[dry-run] would strip dead miner env lines {sorted(dropped)} "
            f"from {env_path} (no consumer since the v0.2.1-rc miner images)"
        )
        return
    tmp = env_path.parent / (env_path.name + ".tmp")
    tmp.write_text("\n".join(kept) + "\n")
    env_path.rename(backup)
    tmp.rename(env_path)
    warnings.append(
        f"stripped dead miner env lines {sorted(dropped)} from {env_path} "
        f"(backup: {backup.name})"
    )


def _backfill_validators(lines, bounds, miner, env_vals, notes):
    """Handle the validators key; returns insert lines (possibly empty)."""
    mstart, mend = bounds
    env_validators = [
        u.strip()
        for u in env_vals.get("QUIP_VALIDATORS", "").split(",")
        if u.strip()
    ]
    if miner.get("validators") == []:
        removed = _remove_toml_array(lines, mstart, mend, "validators")
        bounds[1] = mend - removed
        if not env_validators:
            notes.append(
                "removed empty validators list (the miner falls back to "
                '["ws://quip-validator:9944", "ws://127.0.0.1:9944"] when unset)'
            )
    if env_validators and not miner.get("validators"):
        notes.append("wrote validators from the .env QUIP_VALIDATORS value")
        return [
            "validators = ["
            + ", ".join(_emit_string(u) for u in env_validators)
            + "]"
        ]
    return []


def _backfill_faucet(miner, env_vals, notes, warnings):
    """Handle faucet_url; returns insert lines (possibly empty)."""
    if "faucet_url" in miner:
        return []
    if env_vals.get("QUIP_FAUCET_URL") == "":
        warnings.append(
            ".env had QUIP_FAUCET_URL= (auto-fund opt-out); leaving faucet_url "
            "unset. NOTE: a re-run of this converter would add the testnet "
            "default — keep faucet_url absent manually if you re-run."
        )
        return []
    url = env_vals.get("QUIP_FAUCET_URL") or FAUCET_TESTNET_URL
    notes.append(f"added faucet_url = {url}")
    return [f"faucet_url = {_emit_string(url)}"]


def _backfill_rest(lines, bounds, miner, notes, warnings):
    """Handle rest_port / rest_host; returns insert lines (possibly empty)."""
    mstart, mend = bounds
    inserts = []
    rest_port = miner.get("rest_port")
    if rest_port in (-1, 80):
        pat = re.compile(r"^\s*rest_port\s*=")
        for i in range(mstart, mend):
            if pat.match(lines[i]):
                lines[i] = f"rest_port = {CADDY_PROXY_REST_PORT}"
                break
        notes.append(
            f"rest_port {rest_port} -> {CADDY_PROXY_REST_PORT} "
            f"(Caddy proxies quip-miner:{CADDY_PROXY_REST_PORT})"
        )
    elif rest_port is None:
        inserts.append(f"rest_port = {CADDY_PROXY_REST_PORT}")
        notes.append(f"added rest_port = {CADDY_PROXY_REST_PORT}")
    elif rest_port != CADDY_PROXY_REST_PORT:
        warnings.append(
            f"[miner].rest_port={rest_port} does not match the Caddy proxy "
            f"port {CADDY_PROXY_REST_PORT}; edit caddy/Caddyfile or rest_port "
            "so they agree."
        )
    if "rest_host" not in miner:
        inserts.append('rest_host = "0.0.0.0"')
        notes.append("added rest_host = 0.0.0.0")
    elif miner["rest_host"] != "0.0.0.0":
        warnings.append(
            f"[miner].rest_host={miner['rest_host']!r} may be unreachable from "
            "Caddy inside the compose network; 0.0.0.0 is the expected value."
        )
    return inserts


def _backfill_v02(config_path, parsed, env_vals, dry_run, warnings):
    """Backfill an existing v0.2 config for the config-driven miner images.

    The quip-protocol v0.2.1-rc images dropped every configuration env
    var, so knobs older stacks supplied via QUIP_* env (validators,
    faucet_url, rest_port) must live in config.toml now. Edits are
    line-based to preserve operator comments. Returns True when the file
    was (or, under --dry-run, would be) modified.
    """
    miner = parsed["miner"]
    lines = config_path.read_text().splitlines()
    bounds = list(_miner_bounds(lines))
    notes = []

    inserts = _backfill_validators(lines, bounds, miner, env_vals, notes)
    inserts += _backfill_faucet(miner, env_vals, notes, warnings)
    inserts += _backfill_rest(lines, bounds, miner, notes, warnings)
    if inserts:
        lines[bounds[0] + 1 : bounds[0] + 1] = inserts

    if not notes:
        return False
    if dry_run:
        warnings.append("[dry-run] config backfill: " + "; ".join(notes))
        return True

    backup = config_path.parent / CONFIG_BACKFILL_BACKUP
    if not backup.exists():
        shutil.copy2(config_path, backup)
    config_path.write_text("\n".join(lines) + "\n")
    warnings.append(
        f"config backfill (backup: {backup.name}): " + "; ".join(notes)
    )
    return True


def main():
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "data_dir",
        type=Path,
        help="Operator's data/ directory (containing v0.1 config.toml).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned actions and the new config.toml to stdout; "
        "don't touch the filesystem.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Path to .env to migrate alongside the config (default: "
        "<data_dir>/../.env if present; skip if absent). Use --no-env-file "
        "to skip the .env migration entirely.",
    )
    parser.add_argument(
        "--no-env-file",
        action="store_true",
        help="Skip the .env migration even if a file is detected.",
    )
    args = parser.parse_args()

    data_dir = args.data_dir
    if not data_dir.is_dir():
        sys.stderr.write(f"error: {data_dir} is not a directory.\n")
        sys.exit(1)

    config_path = data_dir / "config.toml"
    if not config_path.is_file():
        sys.stderr.write(f"error: {config_path} not found.\n")
        sys.exit(1)

    try:
        with config_path.open("rb") as fh:
            parsed = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        sys.stderr.write(f"error: failed to parse {config_path}: {exc}\n")
        sys.exit(2)

    schema = _detect_schema(parsed, config_path)
    if schema == "v0.2":
        # Already the v0.2 [miner] schema — backfill the keys that used to
        # arrive via QUIP_* env vars (gone as of the v0.2.1-rc images) and
        # strip the dead lines from .env.
        warnings = []
        env_path = None
        if not args.no_env_file:
            env_path = args.env_file or data_dir.parent / ".env"
        env_vals, kept, dropped = _read_dead_env(env_path)
        changed = _backfill_v02(config_path, parsed, env_vals, args.dry_run, warnings)
        if env_path is not None:
            _write_stripped_env(env_path, kept, dropped, args.dry_run, warnings)
        for w in warnings:
            sys.stderr.write(f"WARN: {w}\n")
        if changed:
            print(f"{config_path}: backfilled config-driven keys (details above).")
        else:
            print(f"{config_path} is already config-driven v0.2. Nothing to do.")
        sys.exit(0)

    warnings = []
    new_content = _render_config(parsed, warnings)

    backup_dir = _backup(data_dir, args.dry_run)

    if args.dry_run:
        print(f"\n[dry-run] would write {config_path} with the following content:\n")
        print(new_content)
    else:
        config_path.write_text(new_content)
        print(f"backed up v0.1 config to {backup_dir}")
        print(f"wrote v0.2 config to {config_path}")

    if not args.no_env_file:
        env_path = args.env_file or data_dir.parent / ".env"
        _upgrade_env_file(env_path, args.dry_run, warnings)

    for w in warnings:
        sys.stderr.write(f"WARN: {w}\n")

    print(
        "\nReview the new config against the canonical v0.2 template at "
        "data/config.toml in the nodes.quip.network repo for inline "
        "documentation. Comments from your v0.1 file were not preserved."
    )


if __name__ == "__main__":
    main()
