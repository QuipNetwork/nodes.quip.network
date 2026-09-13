"""Tests for the merged stack logging configuration and supervisor.

Tasks 1-2 are covered by static assertions on the syslog-ng config file and
entrypoint script (executable bit, signal handling, rotation logic). The
tests below Task 4 start real containers and push output through the actual
Docker syslog driver, proving the merged-file behavior end to end rather
than just matching config text.
"""

import os
import re
import socket
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSLOG_CONF = REPO_ROOT / "syslog-ng" / "syslog-ng.conf"
COLLECTOR_IMAGE = "linuxserver/syslog-ng:4.11.0"
TEST_PORT = 5515


def test_syslog_conf_sets_owner_group_and_perm():
    text = SYSLOG_CONF.read_text()
    assert 'port(5514)' in text
    # so-rcvbuf narrows (does not eliminate) UDP line loss under a burst.
    assert "so-rcvbuf(8388608)" in text
    assert "/logs/quip-node.log" in text
    # Backtick expansion, not a hardcoded literal: the file must be owned by
    # whatever PUID/PGID the operator configures, not always 1000/1000.
    assert "owner(`PUID`)" in text
    assert "group(`PGID`)" in text
    assert "perm(0644)" in text
    # Replacing CR and LF is what stops a forged datagram injecting a second
    # line that impersonates another service. The options stay narrow so the
    # guard does not also rewrite "/" and tabs; see syslog-ng.conf for why the
    # character set must be single-quoted.
    assert r"$(sanitize --no-ctrl-chars --invalid-chars '\n\r' ${MESSAGE})" in text
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


def test_entrypoint_restarts_syslog_ng_when_the_log_is_unlinked():
    """Static check for C1: SIGHUP cannot reopen an unlinked path, so the
    supervisor must kill and restart the syslog-ng child instead."""
    text = ENTRYPOINT.read_text()
    assert 'elif [ "$SEEN" -eq 1 ]' in text, "must distinguish 'never existed yet' from 'deleted'"
    assert 'kill -TERM "$SNG"' in text
    assert "syslog-ng -F -f /config/syslog-ng.conf &" in text.split("elif", 1)[1], (
        "the restart branch must relaunch syslog-ng, not just kill it"
    )


def test_entrypoint_chowns_the_log_dir_and_reports_a_crash():
    text = ENTRYPOINT.read_text()
    # M2: a fresh bind mount is root-owned, blocking non-root archive/cleanup.
    assert "chown" in text and "/logs" in text
    # M4: exit 0 on a syslog-ng crash misreports success.
    assert 'if [ "$RUNNING" -eq 1 ]' in text
    assert "exit 1" in text


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
    # M5(a): nothing else in the suite pins these two down. Deleting both
    # from docker-compose.yml's x-logging anchor leaves every other test
    # green while silently killing QUIP_LOG_MAX_SIZE/QUIP_LOG_MAX_FILE.
    assert "cache-max-size: 32m" in config
    assert 'cache-max-file: "5"' in config


def test_log_port_override_changes_the_published_port_and_syslog_address():
    """C2: QUIP_LOG_PORT must move both the host bind and every producer's
    syslog-address together, so an operator can escape a port collision
    without editing tracked files."""
    env = dict(os.environ, QUIP_LOG_PORT="5599")
    result = subprocess.run(
        ["docker", "compose", "--profile", "cpu", "--profile", "cuda", "--profile", "faucet", "config"],
        cwd=REPO_ROOT, capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, result.stderr
    config = result.stdout
    assert "syslog-address: udp://127.0.0.1:5599" in config
    block = _service_block(config, "quip-syslog")
    assert re.search(
        r'host_ip: 127\.0\.0\.1\n\s*target: 5514\n\s*published: "5599"\n\s*protocol: udp',
        block,
    ), "host port must follow QUIP_LOG_PORT while the container side stays 5514"


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


def _emit(tag, message):
    subprocess.run([
        "docker", "run", "--rm",
        "--log-driver", "syslog",
        "--log-opt", f"syslog-address=udp://127.0.0.1:{TEST_PORT}",
        "--log-opt", f"tag={tag}",
        "alpine:3.22", "sh", "-c", f"echo '{message}'",
    ], check=True, capture_output=True)


def _emit_raw(payload):
    """Send one datagram straight at the collector.

    `_emit` goes through Docker's syslog driver, which splits its input on
    newlines and so cannot express the forgery the sanitize call guards
    against. A local process faces no such limit, which is the threat.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(
            ("<30>Sep 13 10:00:00 host quip-cpu: " + payload).encode(),
            ("127.0.0.1", TEST_PORT),
        )
    finally:
        sock.close()


def _wait_until_collector_is_ready(logs, timeout=10):
    """Poll for a probe line instead of a fixed sleep, since a fixed sleep is
    a flake source: it either wastes time or, on a slow host, races the
    supervisor's own startup."""
    merged = logs / "quip-node.log"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _emit("readiness-probe", "probe-ok")
        time.sleep(0.5)
        if merged.exists() and "probe-ok" in merged.read_text():
            return
    pytest.fail(f"collector did not become ready within {timeout}s")


@pytest.fixture
def collector(tmp_path):
    """Run a real collector on a spare port, writing into tmp_path."""
    logs = tmp_path / "logs"
    logs.mkdir()
    conf = tmp_path / "syslog-ng.conf"
    conf.write_text(SYSLOG_CONF.read_text().replace("port(5514)", f"port({TEST_PORT})"))
    name = "quip-syslog-pytest"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    try:
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
        _wait_until_collector_is_ready(logs)
        yield logs
    finally:
        # try/finally, not a post-yield statement: a container created by the
        # `docker run` above but killed by a later failure (e.g. the
        # readiness poll timing out) must still be removed, or it leaks into
        # every later test run under the same fixed name.
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_merged_lines_keep_slashes_and_tabs_but_cannot_be_forged(collector):
    """The sanitize options are narrow on purpose.

    A bare $(sanitize ...) rewrites "/" and every control character to "_",
    which mangles every URL and path the stack logs and collapses Caddy's
    tab-delimited console format. Keeping those readable while still confining
    a record to one physical line is what the option list buys.
    """
    merged = collector / "quip-node.log"

    _emit("quip-cpu", "validators=ws://quip-validator:9944 dir=/data/logs")
    _emit_raw("2026/08/16 05:22:02.881\tERROR\thttp.log.error\tconnection refused")
    _emit_raw("safe\n2026-09-13T00:00:00+00:00 quip-validator FORGED-LINE")
    time.sleep(2)

    text = merged.read_text()
    assert "ws://quip-validator:9944 dir=/data/logs" in text, "slashes were rewritten"
    assert "05:22:02.881\tERROR\thttp.log.error" in text, "tabs were rewritten"

    # Every record was tagged quip-cpu or readiness-probe. A quip-validator
    # line could only exist if the embedded newline split the record in two.
    programs = {
        line.split(" ", 2)[1] for line in text.splitlines() if line.count(" ") >= 2
    }
    assert "quip-validator" not in programs, (
        f"newline injection forged a line under another service: {sorted(programs)}"
    )
    assert "safe_2026-09-13T00:00:00+00:00 quip-validator FORGED-LINE" in text


def test_multiple_tagged_sources_merge_into_one_host_readable_file(collector):
    """Three synthetic tags stand in for the real services; the full
    seven-service stack is exercised in a later task's integration test."""
    for tag, message in [
        ("quip-miner", "attempt submitted"),
        ("quip-validator", "block imported"),
        ("quip-caddy", "request served"),
    ]:
        _emit(tag, message)
    time.sleep(2)

    merged = collector / "quip-node.log"
    text = merged.read_text()

    assert "quip-miner attempt submitted" in text
    assert "quip-validator block imported" in text
    assert "quip-caddy request served" in text
    assert oct(merged.stat().st_mode)[-3:] == "644"
    # perm(0644) alone makes the file world-readable regardless of who owns
    # it, so it does not prove the backtick PUID/PGID expansion worked -- a
    # config with no owner()/group() at all still passes read_text() above.
    # Check ownership explicitly against the ids the fixture passed in.
    assert merged.stat().st_uid == os.getuid()
    assert merged.stat().st_gid == os.getgid()
    # One merged stream, so the three tags share one file in full arrival
    # order, not just first-before-last.
    assert text.index("quip-miner") < text.index("quip-validator") < text.index("quip-caddy")


def test_container_name_tag_expands_in_the_merged_file(collector):
    """Compose configures `tag: "{{.Name}}"`; confirm Docker's syslog driver
    actually expands that placeholder to the container name, rather than
    leaving it literal or substituting something else."""
    name = "quip-tag-check"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    try:
        subprocess.run([
            "docker", "run", "--rm", "--name", name,
            "--log-driver", "syslog",
            "--log-opt", f"syslog-address=udp://127.0.0.1:{TEST_PORT}",
            "--log-opt", "tag={{.Name}}",
            "alpine:3.22", "sh", "-c", "echo tag-expansion-check",
        ], check=True, capture_output=True)
        time.sleep(2)
        text = (collector / "quip-node.log").read_text()
        assert f"{name} tag-expansion-check" in text
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


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
    # Control case first: a single short line, well under the 4096-byte
    # threshold, held through several 2s check intervals. A mutant that
    # rotates on every interval regardless of size (e.g.
    # `if [ -f "$LOG" ]; then rotate; fi`) would still produce a correct-
    # looking .1-.5 set once the bulk case below runs, so this has to be
    # checked on its own: no rotation must happen from time passing alone.
    _emit(tag="quip-miner", message="tiny-line-under-threshold")
    time.sleep(8)
    assert not (collector / "quip-node.log.1").exists(), (
        "rotation must trigger on size, not merely on the check interval elapsing"
    )

    for round_no in range(1, 8):
        _emit_bulk("quip-miner", f"round-{round_no}")
        time.sleep(4)
    time.sleep(2)

    assert (collector / "quip-node.log").exists(), "live file must be recreated after SIGHUP"
    assert (collector / "quip-node.log.1").exists()
    assert (collector / "quip-node.log.2").exists()
    assert (collector / "quip-node.log.3").exists()
    assert (collector / "quip-node.log.4").exists()
    assert (collector / "quip-node.log.5").exists()
    assert not (collector / "quip-node.log.6").exists(), "KEEP=5 must drop the oldest"

    # The live file must keep taking writes after a rotation.
    _emit(tag="quip-caddy", message="post-rotate-alive")
    time.sleep(2)
    assert "post-rotate-alive" in (collector / "quip-node.log").read_text()


def test_collector_recovers_after_the_live_log_is_deleted(collector):
    """C1, exercised for real: syslog-ng holds the log open, so `[ -f "$LOG" ]`
    alone can never see a deletion. Delete the live file, emit more lines, and
    confirm the supervisor restarts syslog-ng and the file comes back and
    keeps growing -- not just reappears once and stalls."""
    _emit(tag="quip-miner", message="before-delete")
    time.sleep(2)
    merged = collector / "quip-node.log"
    assert merged.exists()

    merged.unlink()
    assert not merged.exists()

    # QUIP_LOG_CHECK_INTERVAL=2 in the fixture; give the supervisor a few
    # cycles to notice the deletion and restart syslog-ng.
    deadline = time.monotonic() + 12
    recreated = False
    while time.monotonic() < deadline:
        _emit(tag="quip-miner", message="after-delete")
        time.sleep(1)
        if merged.exists():
            recreated = True
            break
    assert recreated, "supervisor must recreate the log file after it is deleted"

    size_after_recreate = merged.stat().st_size
    _emit(tag="quip-miner", message="still-growing")
    time.sleep(2)
    assert merged.stat().st_size > size_after_recreate, (
        "the recreated file must keep taking writes, not just exist once"
    )
    assert "still-growing" in merged.read_text()


def test_supervisor_exits_nonzero_when_syslog_ng_crashes():
    """M4: `restart: unless-stopped` recovers a crashed syslog-ng, but the
    supervisor's own exit code must not misreport that as a clean stop."""
    name = "quip-syslog-crashtest"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    try:
        subprocess.run([
            "docker", "run", "-d", "--name", name,
            "-e", "QUIP_LOG_CHECK_INTERVAL=1",
            "-v", f"{SYSLOG_CONF}:/config/syslog-ng.conf:ro",
            "-v", f"{ENTRYPOINT}:/entrypoint.sh:ro",
            "--entrypoint", "/entrypoint.sh", COLLECTOR_IMAGE,
        ], check=True, capture_output=True)
        time.sleep(2)

        pid = subprocess.run(
            ["docker", "exec", name, "pidof", "syslog-ng"],
            capture_output=True, text=True,
        ).stdout.split()[0]
        subprocess.run(["docker", "exec", name, "kill", "-9", pid], check=True, capture_output=True)

        deadline = time.monotonic() + 15
        exit_code = None
        while time.monotonic() < deadline:
            running = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", name],
                capture_output=True, text=True,
            ).stdout.strip()
            if running == "false":
                exit_code = subprocess.run(
                    ["docker", "inspect", "-f", "{{.State.ExitCode}}", name],
                    capture_output=True, text=True,
                ).stdout.strip()
                break
            time.sleep(1)
        assert exit_code is not None, "supervisor must exit after its child dies"
        assert exit_code != "0", "a crashed syslog-ng must not report a clean exit"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_collector_stops_within_the_docker_timeout():
    """Regression: a foreground sleep swallows SIGTERM for the full 10s."""
    name = "quip-syslog-stoptest"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    try:
        subprocess.run([
            "docker", "run", "-d", "--name", name,
            "-e", "QUIP_LOG_CHECK_INTERVAL=30",
            "-v", f"{SYSLOG_CONF}:/config/syslog-ng.conf:ro",
            "-v", f"{ENTRYPOINT}:/entrypoint.sh:ro",
            "--entrypoint", "/entrypoint.sh", COLLECTOR_IMAGE,
        ], check=True, capture_output=True)
        time.sleep(3)
        running = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        # If the supervisor already exited (e.g. a config error), `docker
        # stop` on an exited container returns almost instantly and this
        # test would pass without ever exercising the SIGTERM path.
        assert running == "true", "supervisor must still be running before timing the stop"

        start = time.monotonic()
        subprocess.run(["docker", "stop", name], check=True, capture_output=True)
        elapsed = time.monotonic() - start
        assert elapsed < 5, f"SIGTERM ignored; docker had to SIGKILL after {elapsed:.0f}s"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_docker_logs_reads_the_local_cache_but_fails_when_disabled(collector):
    """Dual logging: `docker logs` reads the local json-file cache the
    syslog driver keeps alongside forwarding, independent of the merged
    file. The negative case (cache-disabled) proves the positive case is
    actually exercising that cache and not just an exit code that would be
    zero regardless of whether reading logs works at all."""
    name = "quip-dual-log-check"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    try:
        subprocess.run([
            "docker", "run", "-d", "--name", name,
            "--log-driver", "syslog",
            "--log-opt", f"syslog-address=udp://127.0.0.1:{TEST_PORT}",
            "alpine:3.22", "sh", "-c", "echo dual-logging-check; sleep 5",
        ], check=True, capture_output=True)
        time.sleep(1)
        result = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
        assert result.returncode == 0
        assert "dual-logging-check" in result.stdout
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    disabled_name = "quip-dual-log-disabled"
    subprocess.run(["docker", "rm", "-f", disabled_name], capture_output=True)
    try:
        subprocess.run([
            "docker", "run", "-d", "--name", disabled_name,
            "--log-driver", "syslog",
            "--log-opt", f"syslog-address=udp://127.0.0.1:{TEST_PORT}",
            "--log-opt", "cache-disabled=true",
            "alpine:3.22", "sh", "-c", "echo dual-logging-check; sleep 5",
        ], check=True, capture_output=True)
        time.sleep(1)
        result = subprocess.run(["docker", "logs", disabled_name], capture_output=True, text=True)
        assert result.returncode != 0, (
            "cache-disabled must make `docker logs` fail; if it still succeeds, "
            "this test cannot tell a working local cache from a broken one"
        )
        assert "does not support reading" in result.stderr
    finally:
        subprocess.run(["docker", "rm", "-f", disabled_name], capture_output=True)
