"""Behavioural tests for systemd/charger-pi/self_update.sh.

The script runs as root on a real host and had two design bugs nobody
caught by reading it (version marker written before the restart was
verified; a swap that was not actually atomic). So it gets tested the
same way the Python does: run the real script under `sh` against fake
`curl` and `systemctl` binaries on PATH, with the install root and
systemd unit directory pointed at a temp tree via its config file.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "systemd" / "charger-pi" / "self_update.sh"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")

FAKE_CURL = r"""#!/bin/sh
# Fake curl: honours -o OUT, treats the last argument as the URL.
out=""
url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
echo "$url" >> "$FAKE_LOG_DIR/curl.log"
# The real script must never let curl write into the CWD - insist on -o.
if [ -z "$out" ]; then echo "fake curl: no -o given for $url" >&2; exit 2; fi
case "$url" in
  *pyproject.toml) cp "$FAKE_FIXTURES/remote_pyproject.toml" "$out" ;;
  *.tar.gz) cp "$FAKE_FIXTURES/main.tar.gz" "$out" ;;
  *health*)
    if [ -f "$FAKE_FIXTURES/health_body.txt" ]; then
      cat "$FAKE_FIXTURES/health_body.txt" > "$out"
    fi
    exit "${FAKE_HEALTH_EXIT:-0}" ;;
  *) echo "fake curl: unexpected url $url" >&2; exit 22 ;;
esac
"""

FAKE_SYSTEMCTL = r"""#!/bin/sh
echo "$*" >> "$FAKE_LOG_DIR/systemctl.log"
case "$1" in
  restart) exit "${FAKE_RESTART_EXIT:-0}" ;;
  show) printf '%s\n' "$FAKE_EXECSTART" ;;
  *) exit 0 ;;
esac
"""


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _make_tarball(dest: Path, version: str, unit_text: str) -> None:
    """Build a main.tar.gz shaped like GitHub's archive of the repo."""
    tree = dest.parent / "tree" / "b6charger-ctl-main"
    if tree.parent.exists():
        shutil.rmtree(tree.parent)
    (tree / "b6charger").mkdir(parents=True)
    (tree / "b6charger" / "cli.py").write_text(f"# cli {version}\n")
    (tree / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n')
    units = tree / "systemd" / "charger-pi"
    units.mkdir(parents=True)
    (units / "b6charger-httpd.service").write_text(unit_text)
    (units / "b6charger-self-update.service").write_text("[Service]\nType=oneshot\n")
    (units / "b6charger-self-update.timer").write_text("[Timer]\nOnBootSec=5min\n")
    (units / "self_update.sh").write_text("#!/bin/sh\n")
    (units / "not-ours.service").write_text("[Service]\nExecStart=/bin/evil\n")
    with tarfile.open(dest, "w:gz") as tf:
        tf.add(tree, arcname="b6charger-ctl-main")


class Harness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.fixtures = root / "fixtures"
        self.logs = root / "logs"
        self.install = root / "opt" / "b6charger-ctl"
        self.systemd = root / "etc" / "systemd" / "system"
        for d in (self.bin, self.fixtures, self.logs, self.install, self.systemd):
            d.mkdir(parents=True)
        _write_exec(self.bin / "curl", FAKE_CURL)
        _write_exec(self.bin / "systemctl", FAKE_SYSTEMCTL)
        self.config = root / "self-update.conf"
        self.config.write_text(
            "\n".join(
                [
                    f'INSTALL_DIR="{self.install}"',
                    f'SYSTEMD_DIR="{self.systemd}"',
                    'HEALTH_URL="http://127.0.0.1:1/health"',
                    'HEALTH_TIMEOUT_S="1"',
                    "",
                ]
            )
        )
        self.marker = self.install / "b6charger_ctl_version.txt"

    # --- state setup -----------------------------------------------------
    def install_local(self, version: str, unit_text: str = "[Service]\nUser=tom\n") -> None:
        src = self.install / "src"
        (src / "b6charger").mkdir(parents=True)
        (src / "b6charger" / "cli.py").write_text(f"# cli {version}\n")
        (src / "pyproject.toml").write_text(f'version = "{version}"\n')
        self.marker.write_text(f"{version}\n")
        (self.systemd / "b6charger-httpd.service").write_text(unit_text)

    def set_remote(self, version: str, unit_text: str = "[Service]\nUser=tom\n") -> None:
        (self.fixtures / "remote_pyproject.toml").write_text(
            f'[project]\nname = "x"\nversion = "{version}"\n'
        )
        _make_tarball(self.fixtures / "main.tar.gz", version, unit_text)

    # --- run -------------------------------------------------------------
    def run(self, *args: str, **env_overrides: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FAKE_LOG_DIR": str(self.logs),
            "FAKE_FIXTURES": str(self.fixtures),
            "FAKE_EXECSTART": "/usr/bin/python3 -m b6charger.cli serve --port 9101",
            "B6_SELF_UPDATE_CONFIG": str(self.config),
            "HOME": str(self.root),
        }
        env.update(env_overrides)
        return subprocess.run(
            ["sh", str(SCRIPT), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def systemctl_calls(self) -> list[str]:
        log = self.logs / "systemctl.log"
        return log.read_text().splitlines() if log.exists() else []

    def deployed_version(self) -> str:
        return (self.install / "src" / "b6charger" / "cli.py").read_text().strip()


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


# --- no-op paths --------------------------------------------------------------


def test_up_to_date_does_nothing(harness):
    harness.install_local("0.9.0")
    harness.set_remote("0.9.0")
    result = harness.run()
    assert result.returncode == 0, result.stderr
    assert "up to date" in result.stdout
    assert harness.systemctl_calls() == []
    assert harness.deployed_version() == "# cli 0.9.0"


def test_check_flag_reports_versions_without_deploying(harness):
    harness.install_local("0.9.0")
    harness.set_remote("0.9.1")
    result = harness.run("--check")
    assert result.returncode == 0, result.stderr
    assert "0.9.0" in result.stdout and "0.9.1" in result.stdout
    assert harness.deployed_version() == "# cli 0.9.0"
    assert harness.systemctl_calls() == []


def test_downgrade_is_refused_by_default(harness):
    harness.install_local("0.9.1")
    harness.set_remote("0.9.0")
    result = harness.run()
    assert result.returncode == 0, result.stderr
    assert "downgrade" in result.stdout.lower()
    assert harness.deployed_version() == "# cli 0.9.1"
    assert harness.marker.read_text().strip() == "0.9.1"


# --- successful update ----------------------------------------------------------


def test_update_swaps_src_writes_marker_and_restarts(harness):
    harness.install_local("0.9.0")
    harness.set_remote("0.9.1")
    result = harness.run()
    assert result.returncode == 0, result.stderr + result.stdout
    assert harness.deployed_version() == "# cli 0.9.1"
    assert harness.marker.read_text().strip() == "0.9.1"
    calls = harness.systemctl_calls()
    assert any(c.startswith("restart b6charger-httpd") for c in calls)
    assert not (harness.install / "src.prev").exists()
    # nothing stray left at the install root (the 2026-08-03 shadow-copy rule)
    leftovers = {p.name for p in harness.install.iterdir()}
    assert leftovers == {"src", "b6charger_ctl_version.txt"}


def test_update_syncs_only_allowlisted_units(harness):
    harness.install_local("0.9.0", unit_text="[Service]\nUser=tom\n")
    harness.set_remote("0.9.1", unit_text="[Service]\nUser=tom\nNoNewPrivileges=true\n")
    result = harness.run()
    assert result.returncode == 0, result.stderr + result.stdout
    live = (harness.systemd / "b6charger-httpd.service").read_text()
    assert "NoNewPrivileges=true" in live
    assert (harness.systemd / "b6charger-self-update.timer").exists()
    assert not (harness.systemd / "not-ours.service").exists()
    assert "daemon-reload" in harness.systemctl_calls()


def test_unchanged_units_do_not_trigger_daemon_reload(harness):
    unit = "[Service]\nUser=tom\n"
    harness.install_local("0.9.0", unit_text=unit)
    (harness.systemd / "b6charger-self-update.service").write_text("[Service]\nType=oneshot\n")
    (harness.systemd / "b6charger-self-update.timer").write_text("[Timer]\nOnBootSec=5min\n")
    harness.set_remote("0.9.1", unit_text=unit)
    result = harness.run()
    assert result.returncode == 0, result.stderr + result.stdout
    assert "daemon-reload" not in harness.systemctl_calls()


# --- host config is captured into a drop-in, out of the shipped unit --------------


def test_first_update_bootstraps_host_dropin_from_live_unit(harness):
    live_unit = "\n".join(
        [
            "[Unit]",
            "Description=x",
            "[Service]",
            "User=tom",
            "WorkingDirectory=/opt/b6charger-ctl/src",
            "Environment=B6CTL_PACKS=/opt/b6charger-ctl/packs.toml",
            "ExecStart=/usr/bin/python3 -m b6charger.cli serve --port 9101",
            "",
        ]
    )
    harness.install_local("0.9.0", unit_text=live_unit)
    harness.set_remote("0.9.1")
    result = harness.run()
    assert result.returncode == 0, result.stderr + result.stdout
    dropin = harness.systemd / "b6charger-httpd.service.d" / "10-host.conf"
    text = dropin.read_text()
    assert "User=tom" in text
    assert "Environment=B6CTL_PACKS=/opt/b6charger-ctl/packs.toml" in text
    assert "ExecStart=\nExecStart=/usr/bin/python3 -m b6charger.cli serve --port 9101" in text


def test_existing_dropin_is_never_overwritten(harness):
    harness.install_local("0.9.0")
    dropin_dir = harness.systemd / "b6charger-httpd.service.d"
    dropin_dir.mkdir()
    (dropin_dir / "10-host.conf").write_text("[Service]\nUser=someone_else\n")
    harness.set_remote("0.9.1")
    result = harness.run()
    assert result.returncode == 0, result.stderr + result.stdout
    assert (dropin_dir / "10-host.conf").read_text() == "[Service]\nUser=someone_else\n"


# --- failure paths: never leave the host broken or stuck ---------------------------


def test_failed_restart_rolls_back_and_leaves_marker_untouched(harness):
    harness.install_local("0.9.0", unit_text="[Service]\nUser=tom\n")
    harness.set_remote("0.9.1", unit_text="[Service]\nUser=broken\n")
    result = harness.run(FAKE_RESTART_EXIT="1")
    assert result.returncode != 0
    assert harness.deployed_version() == "# cli 0.9.0"
    assert harness.marker.read_text().strip() == "0.9.0"
    assert (harness.systemd / "b6charger-httpd.service").read_text() == "[Service]\nUser=tom\n"
    assert not (harness.install / "src.prev").exists()
    restarts = [c for c in harness.systemctl_calls() if c.startswith("restart")]
    assert len(restarts) == 2  # the failed one, then the rollback restart


def test_failed_health_check_rolls_back(harness):
    harness.install_local("0.9.0")
    harness.set_remote("0.9.1")
    result = harness.run(FAKE_HEALTH_EXIT="7")
    assert result.returncode != 0
    assert "health" in (result.stdout + result.stderr).lower()
    assert harness.deployed_version() == "# cli 0.9.0"
    assert harness.marker.read_text().strip() == "0.9.0"


def test_tarball_version_mismatch_aborts_before_touching_src(harness):
    harness.install_local("0.9.0")
    harness.set_remote("0.9.2")
    # remote pyproject says 0.9.2 but the tarball we hand back is 0.9.1
    _make_tarball(harness.fixtures / "main.tar.gz", "0.9.1", "[Service]\nUser=tom\n")
    result = harness.run()
    assert result.returncode != 0
    assert harness.deployed_version() == "# cli 0.9.0"
    assert harness.systemctl_calls() == []


def test_temp_dir_lives_under_install_root_for_same_filesystem_rename(harness):
    harness.install_local("0.9.0")
    harness.set_remote("0.9.1")
    result = harness.run()
    assert result.returncode == 0, result.stderr + result.stdout
    assert str(harness.install) in result.stdout
    assert "/tmp/b6charger-ctl-update" not in result.stdout


# --- optional stricter health: the daemon must actually see the charger ---------


def test_health_can_require_charger_up(harness):
    harness.install_local("0.9.0")
    harness.set_remote("0.9.1")
    harness.config.write_text(harness.config.read_text() + 'HEALTH_REQUIRE_CHARGER_UP="1"\n')
    # fake curl serves the health URL body from this file when asked to
    (harness.fixtures / "health_body.txt").write_text("charger_up 0\n")
    result = harness.run()
    assert result.returncode != 0
    assert "charger_up" in (result.stdout + result.stderr)
    assert harness.deployed_version() == "# cli 0.9.0"
    assert harness.marker.read_text().strip() == "0.9.0"


def test_health_require_charger_up_passes_when_charger_seen(harness):
    harness.install_local("0.9.0")
    harness.set_remote("0.9.1")
    harness.config.write_text(harness.config.read_text() + 'HEALTH_REQUIRE_CHARGER_UP="1"\n')
    (harness.fixtures / "health_body.txt").write_text("charger_up 1\n")
    result = harness.run()
    assert result.returncode == 0, result.stderr + result.stdout
    assert harness.deployed_version() == "# cli 0.9.1"
