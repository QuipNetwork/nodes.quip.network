# Changelog

## v0.2 (unreleased)

### Upgrading from v0.1 — config migration required

The miner config schema changed substantially. Run `make updateconfig` (or `make updateconfig-docker` if the host has Python < 3.11) against your `data/` directory to convert in place. The original files are moved to `data/.v0.1_backup/`; nothing is deleted.

```bash
make updateconfig DATA=path/to/data    # defaults to ./data
```

The converter is idempotent — re-running on an already-converted dir exits cleanly.

#### Schema diff

- **Renamed**: `[global]` → `[miner]`. The catch-all v0.1 section is now scoped to this miner's substrate connection (validator list, keystore, identification).
- **Renamed (binary)**: `quip-node` → `quip-miner`. Example TOML files follow: `quip-node.example.toml` → `quip-miner.example.toml`, `docker/quip-node.{cpu,cuda}.toml` → `docker/quip-miner.{cpu,cuda}.toml`.
- **Added (required)**: `[miner].validators` (ordered failover list of substrate WS URLs), `[miner].signer_key` (path to the sr25519 + ML-DSA-44 hybrid keystore — the entrypoint auto-generates one on first start).
- **Added (optional)**: `[miner].faucet_url`, `[miner].public_host`, `[miner].public_port`.
- **Promoted into `[miner]`**: `log_level`, `node_log` (now rotating 10 MB × 5).
- **Removed (no consumer in v0.2)**: `secret`, `genesis_config`, `auto_mine`, `peer`, `timeout`, `heartbeat_interval`, `heartbeat_timeout`, `fanout`, `verify_tls`, `ca_bundle`, `tls_cert_file`, `tls_key_file`, `rest_tls_cert_file`, `rest_tls_key_file`, `tofu`, `trust_db`, `rest_insecure_port`, `webroot`, `http_log`, `telemetry_enabled`, `telemetry_dir`, and the entire `[telemetry_api]` table. The substrate validator owns p2p, Caddy handles TLS, the REST `/api/v1/*` surface replaces file-based telemetry, and access control is a deployment concern (reverse-proxy auth, network policy) rather than an in-process bearer token.
- **Preserved verbatim**: `[cpu]`, `[gpu]`, `[cuda.N]` / `[nvidia.N]`, `[metal]`, `[modal]`, `[qpu]`, `[dwave]`, `[ibm]`, `[braket]`, `[pasqal]`, `[ionq]`, `[origin]`. Semantics + inheritance rules unchanged.
- **Aliased in the loader (but the converter does NOT use these)**: `[miner].listen` → `[miner].rest_host`, `[miner].port` → `[miner].rest_port`. The aliases exist so a hand-edited file still parses, but the semantics flipped (QUIC peer → telemetry REST). The converter drops `listen` and `port` and emits a warning so an operator with `port = 20049` doesn't accidentally publish the REST API on what used to be the peer port.

#### What the converter does on a v0.1 dir

1. Parses `data/config.toml` with stdlib `tomllib`.
2. Moves every entry in `data/` (except an existing `.v0.1_backup/`) into `data/.v0.1_backup/`.
3. Writes a fresh `data/config.toml` in v0.2 shape, with values harvested from the backed-up file:
   - `node_name`, `public_host`, `public_port`, `rest_host`, `rest_port`, `log_level`, `node_log` carry over.
   - `validators` defaults to `["ws://quip-validator:9944"]` (colocated validator — the common case for `nodes.quip.network`). Override via `.env`'s `QUIP_VALIDATORS` for miner-only or remote deploys.
   - `signer_key` defaults to `"/data/keystore.json"`.
   - All preserved backend tables (`[cpu]`, `[gpu]`, `[cuda.N]`, `[qpu]`, `[dwave]`, ...) are re-serialized verbatim from the parsed dict.
4. Prints operator-actionable warnings to stderr: dropped `port`/`listen` (semantics flipped), dropped `peer[]` (no P2P mesh anymore), `[telemetry_api]` removed, `[dwave].token` preserved but DWAVE_API_KEY in environment is now the convention.

Comments from the v0.1 file are not preserved — stdlib `tomllib` discards them. The canonical v0.2 template at `data/config.toml` ships with inline documentation; reference it after conversion.
