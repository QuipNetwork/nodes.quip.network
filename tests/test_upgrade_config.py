"""End-to-end tests for scripts/upgrade-config.py.

Each test copies a fixture dir into pytest's tmp_path, runs the script
as a subprocess (matching how operators invoke it), then asserts on the
filesystem state and the resulting config.toml. Subprocess + tmp_path
keeps fixtures pristine across runs.
"""

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "upgrade-config.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "v0.1"


def _run(data_dir, *extra_args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(data_dir), *extra_args],
        capture_output=True,
        text=True,
    )


def _copy_fixture(name, tmp_path):
    dst = tmp_path / name
    shutil.copytree(FIXTURES / name, dst)
    return dst / "data"


@pytest.mark.parametrize("fixture", ["cpu", "cuda", "qpu"])
def test_dry_run_leaves_fs_untouched(fixture, tmp_path):
    data_dir = _copy_fixture(fixture, tmp_path)
    before = sorted(p.name for p in data_dir.iterdir())

    result = _run(data_dir, "--dry-run")

    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in data_dir.iterdir()) == before
    assert "[miner]" in result.stdout


@pytest.mark.parametrize("fixture", ["cpu", "cuda", "qpu"])
def test_conversion_produces_valid_v02_config(fixture, tmp_path):
    data_dir = _copy_fixture(fixture, tmp_path)

    result = _run(data_dir)

    assert result.returncode == 0, result.stderr
    backup = data_dir / ".v0.1_backup"
    assert backup.is_dir(), "backup dir not created"
    assert (backup / "config.toml").is_file(), "original config.toml not backed up"

    new_config = (data_dir / "config.toml").read_text()
    parsed = tomllib.loads(new_config)

    assert "miner" in parsed
    assert "global" not in parsed
    assert parsed["miner"]["validators"] == ["ws://quip-validator:9944"]
    assert parsed["miner"]["signer_key"] == "/data/keystore.json"
    assert "rest_host" in parsed["miner"]
    assert "rest_port" in parsed["miner"]


def test_node_name_preserved(tmp_path):
    data_dir = _copy_fixture("cpu", tmp_path)
    _run(data_dir)
    parsed = tomllib.loads((data_dir / "config.toml").read_text())
    assert parsed["miner"]["node_name"] == "cpu-1.carback"


def test_cuda_dotted_tables_preserved(tmp_path):
    data_dir = _copy_fixture("cuda", tmp_path)
    _run(data_dir)
    parsed = tomllib.loads((data_dir / "config.toml").read_text())
    assert parsed["gpu"]["yielding"] is True
    assert parsed["cuda"]["0"]["utilization"] == 50
    assert parsed["cuda"]["1"]["utilization"] == 50


def test_qpu_dwave_table_preserved(tmp_path):
    data_dir = _copy_fixture("qpu", tmp_path)
    result = _run(data_dir)
    parsed = tomllib.loads((data_dir / "config.toml").read_text())
    assert "qpu" in parsed
    assert parsed["dwave"]["solver"] == "Advantage2_system1"
    assert parsed["dwave"]["daily_budget"] == "20m"
    assert parsed["dwave"]["token"] == "DEV-FIXTURE-FAKE-TOKEN-DO-NOT-USE"
    assert "DWAVE_API_KEY" in result.stderr, "missing dwave-token-in-env warning"


def test_telemetry_api_table_dropped(tmp_path):
    data_dir = _copy_fixture("qpu", tmp_path)
    result = _run(data_dir)
    parsed = tomllib.loads((data_dir / "config.toml").read_text())
    assert "telemetry_api" not in parsed
    assert "telemetry_api" in result.stderr


def test_port_drop_warns_loudly(tmp_path):
    data_dir = _copy_fixture("cpu", tmp_path)
    result = _run(data_dir, "--dry-run")
    assert "dropping [global].port=20049" in result.stderr
    assert "dropping [global].listen=" in result.stderr


def test_peer_list_drop_warns(tmp_path):
    data_dir = _copy_fixture("cpu", tmp_path)
    result = _run(data_dir, "--dry-run")
    assert "[global].peer had" in result.stderr
    assert "no P2P mesh" in result.stderr


def test_idempotence_on_already_v02(tmp_path):
    data_dir = _copy_fixture("already-v0.2", tmp_path)
    before = sorted(p.name for p in data_dir.iterdir())

    result = _run(data_dir)

    assert result.returncode == 0
    assert "already v0.2" in result.stdout
    assert sorted(p.name for p in data_dir.iterdir()) == before


def test_readonly_data_dir_emits_chown_hint(tmp_path):
    data_dir = _copy_fixture("cpu", tmp_path)
    original_mode = data_dir.stat().st_mode
    data_dir.chmod(0o555)  # r-x — no write
    try:
        result = _run(data_dir)
    finally:
        data_dir.chmod(original_mode)
    assert result.returncode == 1, result.stderr
    assert "not writable" in result.stderr
    assert "chown -R" in result.stderr


def test_backup_collision_refuses(tmp_path):
    data_dir = _copy_fixture("cpu", tmp_path)
    (data_dir / ".v0.1_backup").mkdir()

    result = _run(data_dir)

    assert result.returncode == 1
    assert "already-migrated" in result.stderr


def test_missing_data_dir(tmp_path):
    result = _run(tmp_path / "nope")
    assert result.returncode == 1
    assert "is not a directory" in result.stderr


def test_missing_config_toml(tmp_path):
    empty = tmp_path / "data"
    empty.mkdir()
    result = _run(empty)
    assert result.returncode == 1
    assert "not found" in result.stderr


def test_unrecognized_config(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "config.toml").write_text('[something_else]\nfoo = "bar"\n')
    result = _run(d)
    assert result.returncode == 2
    assert "not a recognizable" in result.stderr


def test_ambiguous_config(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "config.toml").write_text("[global]\nnode_name = 'x'\n[miner]\nvalidators = []\n")
    result = _run(d)
    assert result.returncode == 2
    assert "ambiguous" in result.stderr


def test_invalid_toml(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "config.toml").write_text("this is = not valid TOML at all = no")
    result = _run(d)
    assert result.returncode == 2
    assert "failed to parse" in result.stderr


def test_double_run_after_migration_is_idempotent(tmp_path):
    data_dir = _copy_fixture("cuda", tmp_path)
    first = _run(data_dir)
    assert first.returncode == 0
    second = _run(data_dir)
    assert second.returncode == 0
    assert "already v0.2" in second.stdout
