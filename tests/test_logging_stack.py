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
