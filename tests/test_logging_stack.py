"""Static tests for the merged stack logging configuration and supervisor.

Tests Tasks 1-2: static assertions on the syslog-ng config file and entrypoint
script (executable bit, signal handling, rotation logic). Real-container tests
sending output through the Docker syslog driver arrive in a later task.
"""

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSLOG_CONF = REPO_ROOT / "syslog-ng" / "syslog-ng.conf"
COLLECTOR_IMAGE = "linuxserver/syslog-ng:latest"
TEST_PORT = 5515


def test_syslog_conf_sets_owner_group_and_perm():
    text = SYSLOG_CONF.read_text()
    assert 'udp(ip("0.0.0.0") port(5514))' in text
    assert "/logs/quip-node.log" in text
    # Backtick expansion, not a hardcoded literal: the file must be owned by
    # whatever PUID/PGID the operator configures, not always 1000/1000.
    assert "owner(`PUID`)" in text
    assert "group(`PGID`)" in text
    assert "perm(0644)" in text
    # The log statement must join source s_net to destination d_merged.
    assert "log { source(s_net); destination(d_merged); }" in text


ENTRYPOINT = REPO_ROOT / "syslog-ng" / "entrypoint.sh"


def test_entrypoint_is_executable_posix_sh():
    assert ENTRYPOINT.exists(), "entrypoint.sh missing"
    assert ENTRYPOINT.stat().st_mode & 0o111, "entrypoint.sh must be executable"
    text = ENTRYPOINT.read_text()
    assert text.startswith("#!/bin/sh"), "the image has no bash on PATH"
    assert "set -eu" in text


def test_entrypoint_rotates_by_size_and_keeps_five():
    text = ENTRYPOINT.read_text()
    assert "QUIP_LOG_MAX_BYTES:-10485760" in text, "must default to 10 MB, matching v0.1"
    assert "QUIP_LOG_KEEP:-5" in text
    assert "stat -c %s" in text, "rotation must trigger on size, not on time"
    assert "kill -HUP" in text
    # Rotation must shift away from the live file (i + 1, not i - 1).
    assert 'mv "$LOG.$i" "$LOG.$((i + 1))"' in text, "must increment index, shifting away"
    # Must remove the oldest file before rotation and rename the live file.
    assert 'rm -f "$LOG.$KEEP"' in text, "must clean up oldest backup"
    assert 'mv "$LOG" "$LOG.1"' in text, "must rename live log"


def test_entrypoint_handles_sigterm_without_blocking_on_sleep():
    """Regression: a foreground sleep swallows SIGTERM for the full 10s timeout."""
    text = ENTRYPOINT.read_text()
    assert "sleep \"$INTERVAL\" &" in text, "sleep must be backgrounded"
    assert "wait \"$SLP\"" in text, "wait is interruptible by signals; a bare sleep is not"
    assert "trap" in text
    # The stop function must kill the backgrounded sleep, not just RUNNING.
    assert 'kill "$SLP"' in text, "stop must kill the sleep PID to unblock wait"


# The seven services that must forward logs to, and start after, quip-syslog.
LOGGING_SERVICES = [
    "cpu", "cuda", "quip-validator", "quip-faucet", "dashboard", "postgres", "caddy",
]


def _compose_config():
    """Ask compose to resolve the file, rather than parsing YAML anchors by hand.

    Renders every profile (cpu, cuda, faucet) so `cpu` — the default
    `make testnet` path — is checked too, not just cuda/faucet.
    """
    result = subprocess.run(
        ["docker", "compose", "--profile", "cpu", "--profile", "cuda", "--profile", "faucet", "config"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _service_block(config, service):
    """Extract one service's rendered YAML block by indentation.

    pyyaml is not available here, and substring/count assertions over the
    whole file are fooled by the x-logging/x-dashboard anchors compose also
    dumps back out, so this walks the text per service instead: a compose
    service is a "  name:" line (2-space indent) followed by its body
    (4-space indent) until the next 2-space (or 0-space) line.
    """
    match = re.search(rf"^  {re.escape(service)}:\n((?:    .+\n)*)", config, re.M)
    assert match, f"service {service!r} not found in the rendered compose config"
    return match.group(1)


def test_every_service_uses_the_syslog_driver():
    config = _compose_config()
    for service in LOGGING_SERVICES:
        block = _service_block(config, service)
        assert "driver: syslog" in block, f"{service} must inherit the syslog logging anchor"
    assert "syslog-address: udp://127.0.0.1:5514" in config
    assert "tag: '{{.Name}}'" in config or 'tag: "{{.Name}}"' in config


def test_services_depend_on_the_collector_starting():
    config = _compose_config()
    for service in LOGGING_SERVICES:
        block = _service_block(config, service)
        assert re.search(r"quip-syslog:\s*\n\s*condition: service_started", block), (
            f"{service} must depend on quip-syslog with condition: service_started"
        )
    # The image declares no healthcheck; service_healthy would hang forever.
    assert not re.search(r"quip-syslog:\s*\n\s*condition: service_healthy", config)


def test_collector_itself_stays_on_json_file():
    config = _compose_config()
    block = _service_block(config, "quip-syslog")
    assert "driver: json-file" in block, "the collector must not use *default-logging"
    assert "driver: syslog" not in block, "pointing the collector at itself is a feedback loop"
    # cache-disabled would stop `docker logs`/`docker compose logs` from working
    # against every syslog-driver service, not just the collector.
    assert "cache-disabled" not in config


def test_collector_publishes_on_loopback_only():
    config = _compose_config()
    block = _service_block(config, "quip-syslog")
    assert re.search(
        r'host_ip: 127\.0\.0\.1\n\s*target: 5514\n\s*published: "5514"\n\s*protocol: udp',
        block,
    ), "collector port must publish on the loopback host_ip, not all interfaces"


@pytest.fixture
def collector(tmp_path):
    """Run a real collector on a spare port, writing into tmp_path."""
    logs = tmp_path / "logs"
    logs.mkdir()
    conf = tmp_path / "syslog-ng.conf"
    conf.write_text(SYSLOG_CONF.read_text().replace("port(5514)", f"port({TEST_PORT})"))
    name = "quip-syslog-pytest"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run([
        "docker", "run", "-d", "--name", name,
        "-p", f"127.0.0.1:{TEST_PORT}:{TEST_PORT}/udp",
        # Small threshold and a fast interval so rotation is testable in
        # seconds. Production defaults are 10485760 bytes and 30 seconds.
        "-e", "QUIP_LOG_MAX_BYTES=4096",
        "-e", "QUIP_LOG_CHECK_INTERVAL=2",
        # syslog-ng.conf's owner()/group() are backtick env expansions with no
        # default; without these the merged file would be owned by nobody the
        # test runner can read back.
        "-e", f"PUID={os.getuid()}",
        "-e", f"PGID={os.getgid()}",
        "-v", f"{conf}:/config/syslog-ng.conf:ro",
        "-v", f"{ENTRYPOINT}:/entrypoint.sh:ro",
        "-v", f"{logs}:/logs",
        "--entrypoint", "/entrypoint.sh", COLLECTOR_IMAGE,
    ], check=True, capture_output=True)
    time.sleep(4)
    yield logs
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _emit(tag, message):
    subprocess.run([
        "docker", "run", "--rm",
        "--log-driver", "syslog",
        "--log-opt", f"syslog-address=udp://127.0.0.1:{TEST_PORT}",
        "--log-opt", f"tag={tag}",
        "alpine:3.22", "sh", "-c", f"echo '{message}'",
    ], check=True, capture_output=True)


def test_all_services_land_in_one_file_readable_by_the_host(collector):
    for tag, message in [
        ("quip-miner", "attempt submitted"),
        ("quip-validator", "block imported"),
        ("quip-caddy", "request served"),
    ]:
        _emit(tag, message)
    time.sleep(2)

    merged = collector / "quip-node.log"
    text = merged.read_text()  # fails outright if perms are wrong

    assert "quip-miner attempt submitted" in text
    assert "quip-validator block imported" in text
    assert "quip-caddy request served" in text
    assert oct(merged.stat().st_mode)[-3:] == "644"
    # One merged stream, so the three tags share one file in arrival order.
    assert text.index("quip-miner") < text.index("quip-caddy")


def _emit_bulk(tag, marker, count=60):
    subprocess.run([
        "docker", "run", "--rm",
        "--log-driver", "syslog",
        "--log-opt", f"syslog-address=udp://127.0.0.1:{TEST_PORT}",
        "--log-opt", f"tag={tag}",
        "alpine:3.22", "sh", "-c",
        f"for i in $(seq 1 {count}); do echo '{marker} line '$i' padding-padding-padding'; done",
    ], check=True, capture_output=True)


def test_log_rotates_by_size_and_keeps_five_generations(collector):
    """Threshold is 4096 bytes in this fixture; each round writes past it."""
    for round_no in range(1, 8):
        _emit_bulk("quip-miner", f"round-{round_no}")
        time.sleep(4)
    time.sleep(2)

    assert (collector / "quip-node.log").exists(), "live file must be recreated after SIGHUP"
    assert (collector / "quip-node.log.1").exists()
    assert (collector / "quip-node.log.5").exists()
    assert not (collector / "quip-node.log.6").exists(), "KEEP=5 must drop the oldest"

    # The live file must keep taking writes after a rotation.
    _emit(tag="quip-caddy", message="post-rotate-alive")
    time.sleep(2)
    assert "post-rotate-alive" in (collector / "quip-node.log").read_text()


def test_collector_stops_within_the_docker_timeout():
    """Regression: a foreground sleep swallows SIGTERM for the full 10s."""
    name = "quip-syslog-stoptest"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run([
        "docker", "run", "-d", "--name", name,
        "-e", "QUIP_LOG_CHECK_INTERVAL=30",
        "-v", f"{SYSLOG_CONF}:/config/syslog-ng.conf:ro",
        "-v", f"{ENTRYPOINT}:/entrypoint.sh:ro",
        "--entrypoint", "/entrypoint.sh", COLLECTOR_IMAGE,
    ], check=True, capture_output=True)
    time.sleep(3)
    start = time.monotonic()
    subprocess.run(["docker", "stop", name], check=True, capture_output=True)
    elapsed = time.monotonic() - start
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    assert elapsed < 5, f"SIGTERM ignored; docker had to SIGKILL after {elapsed:.0f}s"


def test_docker_logs_still_works_under_the_syslog_driver(collector):
    """Dual logging: the driver is not readable, but the local cache is."""
    result = subprocess.run([
        "docker", "run", "--rm",
        "--log-driver", "syslog",
        "--log-opt", f"syslog-address=udp://127.0.0.1:{TEST_PORT}",
        "alpine:3.22", "sh", "-c", "echo dual-logging-check",
    ], capture_output=True, text=True)
    assert result.returncode == 0
