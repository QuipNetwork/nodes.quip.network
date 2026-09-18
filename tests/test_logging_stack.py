"""Tests for the merged stack logging wiring.

The collector (syslog-ng, its config, and its rotation) lives in the
dashboard image and is tested in the dashboard repository
(deploy/tests/logging.sh). These tests cover this repository's side of the
contract: the compose wiring, and a real dashboard container receiving lines
through the Docker syslog driver.

QUIP_DASHBOARD_TEST_IMAGE overrides the image under test. The default is the
image docker-compose.yml resolves for the dashboard service.
"""

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_PORT = 5515


# The services that forward their stdout to the collector in the dashboard
# container. The dashboard itself is not one of them: see
# test_collector_itself_stays_on_json_file.
LOGGING_SERVICES = ["cpu", "cuda", "quip-validator", "quip-faucet"]

# Folded into the dashboard image. Nothing may define or wait on them.
REMOVED_SERVICES = ["quip-syslog", "postgres", "caddy"]


def _compose_config(env=None):
    """Ask compose to resolve the file, rather than parsing YAML anchors by hand.

    Renders every profile (cpu, cuda, faucet) so `cpu` — the default
    `make testnet` path — is checked too, not just cuda/faucet.
    """
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "cpu",
            "--profile",
            "cuda",
            "--profile",
            "faucet",
            "config",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _service_block(config, service):
    """Extract one service's rendered YAML block by indentation.

    pyyaml is not available here, and substring/count assertions over the
    whole file are fooled by the x-logging anchor compose also dumps back
    out, so this walks the text per service instead: a compose service is a
    "  name:" line (2-space indent) followed by its body (4-space indent)
    until the next 2-space (or 0-space) line.
    """
    match = re.search(rf"^  {re.escape(service)}:\n((?:    .+\n)*)", config, re.M)
    assert match, f"service {service!r} not found in the rendered compose config"
    return match.group(1)


def test_every_service_uses_the_syslog_driver():
    config = _compose_config()
    for service in LOGGING_SERVICES:
        block = _service_block(config, service)
        assert "driver: syslog" in block, (
            f"{service} must inherit the syslog logging anchor"
        )
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
    config = _compose_config(dict(os.environ, QUIP_LOG_PORT="5599"))
    assert "syslog-address: udp://127.0.0.1:5599" in config
    block = _service_block(config, "dashboard")
    assert re.search(
        r'host_ip: 127\.0\.0\.1\n\s*target: 5514\n\s*published: "5599"\n\s*protocol: udp',
        block,
    ), "host port must follow QUIP_LOG_PORT while the container side stays 5514"


def test_removed_services_are_gone_and_nothing_waits_on_them():
    config = _compose_config()
    for service in REMOVED_SERVICES:
        assert not re.search(rf"^  {re.escape(service)}:\n", config, re.M), (
            f"{service} now runs inside the dashboard image"
        )
        # A leftover depends_on entry would make compose refuse the file, but
        # only for the profile that pulls the dependent in. Check the text.
        assert not re.search(rf"^      {re.escape(service)}:\n", config, re.M), (
            f"a service still depends on {service}"
        )


def test_dashboard_starts_without_waiting_for_the_validator():
    """The collector and the :20049 front door must come up while the
    validator is still syncing, or the sync output never reaches the merged
    log. The indexer's own sync gate handles a syncing validator."""
    block = _service_block(_compose_config(), "dashboard")
    assert "depends_on" not in block


def test_collector_itself_stays_on_json_file():
    config = _compose_config()
    block = _service_block(config, "dashboard")
    assert "driver: json-file" in block, "the dashboard must not use *default-logging"
    assert "driver: syslog" not in block, (
        "pointing the collector at itself is a feedback loop"
    )
    # cache-disabled would stop `docker logs`/`docker compose logs` from working
    # against every syslog-driver service, not just the collector.
    assert "cache-disabled" not in config


def test_collector_publishes_on_loopback_only():
    block = _service_block(_compose_config(), "dashboard")
    assert re.search(
        r'host_ip: 127\.0\.0\.1\n\s*target: 5514\n\s*published: "5514"\n\s*protocol: udp',
        block,
    ), "collector port must publish on the loopback host_ip, not all interfaces"


def test_dashboard_keeps_certificates_and_the_merged_log_path():
    block = _service_block(_compose_config(), "dashboard")
    # The image sets XDG_DATA_HOME/XDG_CONFIG_HOME to these paths, so the
    # existing volumes keep their certificates at the same relative paths.
    assert re.search(r"source: caddy-data\n\s*target: /data/caddy/data", block)
    assert re.search(r"source: caddy-config\n\s*target: /data/caddy/config", block)
    assert re.search(r"source: \S+/data/logs\n\s*target: /logs\n", block)
    assert re.search(r"source: \S+/dashboard-data\n\s*target: /data\n", block)


def test_dashboard_dials_upstreams_directly_and_uses_turso():
    block = _service_block(_compose_config(), "dashboard")
    assert "QUIP_VALIDATOR_RPC_URLS: ws://quip-validator:9944" in block
    assert "QUIP_MINER_REST_URL: http://quip-miner:8086" in block
    # An empty or absent DATABASE_URL selects the embedded Turso store.
    assert "DATABASE_URL" not in block
    assert "quip-caddy" not in block


def _emit(tag, message):
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--log-driver",
            "syslog",
            "--log-opt",
            f"syslog-address=udp://127.0.0.1:{TEST_PORT}",
            "--log-opt",
            f"tag={tag}",
            "alpine:3.22",
            "sh",
            "-c",
            f"echo '{message}'",
        ],
        check=True,
        capture_output=True,
    )


def _dashboard_image():
    override = os.environ.get("QUIP_DASHBOARD_TEST_IMAGE")
    if override:
        return override
    block = _service_block(_compose_config(), "dashboard")
    match = re.search(r"^    image: (\S+)$", block, re.M)
    assert match, "dashboard service has no image"
    return match.group(1)


def _wait_until_collector_is_ready(logs, timeout=60):
    """Poll for a probe line instead of a fixed sleep. The supervisor starts
    the backend and Caddy alongside syslog-ng, so the first boot is slower
    than a bare collector's."""
    merged = logs / "quip-node.log"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _emit("readiness-probe", "probe-ok")
        time.sleep(1)
        if merged.exists() and "probe-ok" in merged.read_text():
            return
    pytest.fail(f"collector did not become ready within {timeout}s")


@pytest.fixture
def collector(tmp_path):
    """Run the dashboard image on a spare collector port, logging into tmp_path.

    Only the collector port is published. The validator and miner are
    unreachable, which the backend tolerates: it keeps retrying them.
    """
    logs = tmp_path / "logs"
    logs.mkdir()
    name = "quip-dashboard-logtest"
    # -v: the image declares VOLUME /data, so each run creates an anonymous
    # volume that must be removed with the container.
    subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True)
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "-p",
                f"127.0.0.1:{TEST_PORT}:5514/udp",
                "-e",
                f"PUID={os.getuid()}",
                "-e",
                f"PGID={os.getgid()}",
                "-v",
                f"{logs}:/logs",
                _dashboard_image(),
            ],
            check=True,
            capture_output=True,
        )
        _wait_until_collector_is_ready(logs)
        yield logs
    finally:
        # try/finally, not a post-yield statement: a container created by the
        # `docker run` above but killed by a later failure (e.g. the
        # readiness poll timing out) must still be removed, or it leaks into
        # every later test run under the same fixed name.
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True)


def test_multiple_tagged_sources_merge_into_one_host_readable_file(collector):
    """Three synthetic tags stand in for the real services."""
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
    # order. Match whole records: the dashboard's own lines also mention
    # quip-validator (its RPC URL).
    assert (
        text.index("quip-miner attempt submitted")
        < text.index("quip-validator block imported")
        < text.index("quip-caddy request served")
    )


def test_container_name_tag_expands_in_the_merged_file(collector):
    """Compose configures `tag: "{{.Name}}"`; confirm Docker's syslog driver
    actually expands that placeholder to the container name, rather than
    leaving it literal or substituting something else."""
    name = "quip-tag-check"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                name,
                "--log-driver",
                "syslog",
                "--log-opt",
                f"syslog-address=udp://127.0.0.1:{TEST_PORT}",
                "--log-opt",
                "tag={{.Name}}",
                "alpine:3.22",
                "sh",
                "-c",
                "echo tag-expansion-check",
            ],
            check=True,
            capture_output=True,
        )
        time.sleep(2)
        text = (collector / "quip-node.log").read_text()
        assert f"{name} tag-expansion-check" in text
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
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--log-driver",
                "syslog",
                "--log-opt",
                f"syslog-address=udp://127.0.0.1:{TEST_PORT}",
                "alpine:3.22",
                "sh",
                "-c",
                "echo dual-logging-check; sleep 5",
            ],
            check=True,
            capture_output=True,
        )
        time.sleep(1)
        result = subprocess.run(
            ["docker", "logs", name], capture_output=True, text=True
        )
        assert result.returncode == 0
        assert "dual-logging-check" in result.stdout
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    disabled_name = "quip-dual-log-disabled"
    subprocess.run(["docker", "rm", "-f", disabled_name], capture_output=True)
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                disabled_name,
                "--log-driver",
                "syslog",
                "--log-opt",
                f"syslog-address=udp://127.0.0.1:{TEST_PORT}",
                "--log-opt",
                "cache-disabled=true",
                "alpine:3.22",
                "sh",
                "-c",
                "echo dual-logging-check; sleep 5",
            ],
            check=True,
            capture_output=True,
        )
        time.sleep(1)
        result = subprocess.run(
            ["docker", "logs", disabled_name], capture_output=True, text=True
        )
        assert result.returncode != 0, (
            "cache-disabled must make `docker logs` fail; if it still succeeds, "
            "this test cannot tell a working local cache from a broken one"
        )
        assert "does not support reading" in result.stderr
    finally:
        subprocess.run(["docker", "rm", "-f", disabled_name], capture_output=True)
