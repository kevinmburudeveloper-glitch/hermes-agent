"""Inherited systemd markers must not make a manual gateway look supervised.

A ``hermes gateway run`` started from a terminal/IDE inside a systemd *scope* inherits
``INVOCATION_ID`` and friends. Unscrubbed, ``identify`` answers ``systemd``: ``hermes update``
plans a restart of a unit that does not exist, and ``hermes gateway restart`` drains the gateway
and waits for a supervisor that never respawns it. Only a real ``.service`` MainPID (or the
generated-launcher marker) is evidence of supervision.
"""
import os

import pytest

from gateway import restart
from gateway.control_socket import _detect_supervisor


def _clear_markers(monkeypatch):
    for name in (*restart._INHERITED_SUPERVISOR_ENV,
                 "HERMES_SUPERVISED_CHILD", "HERMES_S6_SUPERVISED_CHILD",
                 restart.EXTERNAL_GATEWAY_SUPERVISOR_ENV, "HERMES_DESKTOP_MANAGED"):
        monkeypatch.delenv(name, raising=False)


def _set_inherited(monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "deadbeef" * 4)
    monkeypatch.setenv("JOURNAL_STREAM", "10:4242")
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/user/1000/systemd/notify")


def test_scope_markers_are_scrubbed_and_gateway_reports_manual(monkeypatch):
    _clear_markers(monkeypatch)
    _set_inherited(monkeypatch)
    monkeypatch.setattr(restart, "_self_cgroup_path",
                        lambda: "/user.slice/user-1000.slice/user@1000.service/"
                                "app-graphical.slice/app-IDE-1234.scope")
    # A .scope is never respawned; the real probe (systemctl) is irrelevant here.
    assert restart.systemd_service_owns_this_process() is False

    restart.scrub_inherited_supervisor_markers()

    for name in restart._INHERITED_SUPERVISOR_ENV:
        assert os.environ.get(name) is None
    assert _detect_supervisor() == "manual"


def test_genuine_service_keeps_inherited_markers(monkeypatch):
    _clear_markers(monkeypatch)
    _set_inherited(monkeypatch)
    monkeypatch.setattr(restart, "systemd_service_owns_this_process", lambda unit=None: True)

    restart.scrub_inherited_supervisor_markers()

    assert os.environ["INVOCATION_ID"] == "deadbeef" * 4
    assert _detect_supervisor() == "systemd"


@pytest.mark.parametrize("marker", ["HERMES_SUPERVISED_CHILD", "HERMES_S6_SUPERVISED_CHILD"])
def test_generated_launcher_marker_is_never_scrubbed(monkeypatch, marker):
    _clear_markers(monkeypatch)
    _set_inherited(monkeypatch)
    monkeypatch.setenv(marker, "1")
    monkeypatch.setattr(restart, "systemd_service_owns_this_process", lambda unit=None: False)

    restart.scrub_inherited_supervisor_markers()

    assert os.environ["INVOCATION_ID"] == "deadbeef" * 4
    assert _detect_supervisor() == "systemd"


def test_external_supervisor_opt_in_is_never_scrubbed(monkeypatch):
    _clear_markers(monkeypatch)
    _set_inherited(monkeypatch)
    monkeypatch.setenv(restart.EXTERNAL_GATEWAY_SUPERVISOR_ENV, "1")
    monkeypatch.setattr(restart, "systemd_service_owns_this_process", lambda unit=None: False)

    restart.scrub_inherited_supervisor_markers()

    # Kept: the explicit opt-in proves a supervisor exists even when the markers were inherited.
    assert os.environ["INVOCATION_ID"] == "deadbeef" * 4
    assert os.environ[restart.EXTERNAL_GATEWAY_SUPERVISOR_ENV] == "1"


def test_without_invocation_id_nothing_is_touched(monkeypatch):
    _clear_markers(monkeypatch)
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/user/1000/systemd/notify")
    monkeypatch.setattr(restart, "systemd_service_owns_this_process", lambda unit=None: False)

    restart.scrub_inherited_supervisor_markers()

    assert os.environ.get("NOTIFY_SOCKET") == "/run/user/1000/systemd/notify"


class _Done:
    def __init__(self, stdout):
        self.stdout = stdout
        self.returncode = 0


def _fake_systemctl(main_pid, restart_policy):
    def run(cmd, **_kwargs):
        if "--property=MainPID" in cmd:
            return _Done(f"{main_pid}\n")
        if "--property=Restart" in cmd:
            return _Done(f"{restart_policy}\n")
        raise AssertionError(f"unexpected systemctl call: {cmd}")
    return run


def test_service_owned_by_this_pid_counts_but_scope_does_not(monkeypatch):
    monkeypatch.setattr(restart, "_self_cgroup_path",
                        lambda: "/user.slice/user-1000.slice/user@1000.service/hermes-gateway.service")
    monkeypatch.setattr(restart.subprocess, "run", _fake_systemctl(os.getpid(), "always"))
    assert restart.systemd_service_owns_this_process("hermes-gateway.service") is True

    # A shell inside another unit: its service MainPID is someone else.
    monkeypatch.setattr(restart.subprocess, "run", _fake_systemctl(os.getpid() + 1, "always"))
    assert restart.systemd_service_owns_this_process("hermes-gateway.service") is False

    # The right PID but no restart policy cannot bring the gateway back.
    monkeypatch.setattr(restart.subprocess, "run", _fake_systemctl(os.getpid(), "no"))
    assert restart.systemd_service_owns_this_process("hermes-gateway.service") is False


def test_scope_leaf_is_rejected_without_invoking_systemctl(monkeypatch):
    monkeypatch.setattr(restart, "_self_cgroup_path",
                        lambda: "/user.slice/user-1000.slice/session-2.scope")

    def explode(*_args, **_kwargs):
        raise AssertionError("systemctl must not be consulted for a non-service cgroup")

    monkeypatch.setattr(restart.subprocess, "run", explode)
    assert restart.systemd_service_owns_this_process() is False
