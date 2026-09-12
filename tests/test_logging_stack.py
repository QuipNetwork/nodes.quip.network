"""Static tests for the merged stack logging configuration and supervisor.

Tests Tasks 1-2: static assertions on the syslog-ng config file and entrypoint
script (executable bit, signal handling, rotation logic). Real-container tests
sending output through the Docker syslog driver arrive in a later task.
"""

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
    # syslog-ng does no variable expansion, so these must be numeric literals.
    assert "owner(1000)" in text
    assert "group(1000)" in text
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


def _compose_config():
    """Ask compose to resolve the file, rather than parsing YAML anchors by hand."""
    result = subprocess.run(
        ["docker", "compose", "--profile", "cuda", "--profile", "faucet", "config"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_every_service_uses_the_syslog_driver():
    config = _compose_config()
    assert config.count("driver: syslog") >= 7, "all seven services must inherit the anchor"
    assert "syslog-address: udp://127.0.0.1:5514" in config
    assert "tag: '{{.Name}}'" in config or 'tag: "{{.Name}}"' in config


def test_collector_publishes_on_loopback_only():
    config = _compose_config()
    assert "127.0.0.1" in config, "collector port must not bind all interfaces"
    assert "quip-syslog" in config
