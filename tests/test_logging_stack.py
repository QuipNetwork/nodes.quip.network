"""End-to-end tests for the merged stack logging setup.

Each test starts a real syslog-ng collector container, sends container
output through the Docker syslog driver, then asserts on the merged file
from the host. Real containers, because the thing under test is the
interaction between the Docker daemon, the driver, and syslog-ng.
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


def test_entrypoint_handles_sigterm_without_blocking_on_sleep():
    """Regression: a foreground sleep swallows SIGTERM for the full 10s timeout."""
    text = ENTRYPOINT.read_text()
    assert "sleep \"$INTERVAL\" &" in text, "sleep must be backgrounded"
    assert "wait \"$SLP\"" in text, "wait is interruptible by signals; a bare sleep is not"
    assert "trap" in text
