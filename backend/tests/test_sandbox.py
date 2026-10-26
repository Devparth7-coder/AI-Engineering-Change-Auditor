"""Execution isolation: real subprocesses, real limits, honest probing."""

from __future__ import annotations

import sys

import pytest

from app.services.sandbox import (
    ExecutionRequest,
    cleanup_workspace,
    execute,
    make_workspace,
    probe_isolation,
    resolve_isolation,
)


def test_child_runs_and_reports_exit_code() -> None:
    ws = make_workspace(prefix="unit")
    try:
        result = execute(
            ExecutionRequest(
                argv=[sys.executable, "-c", "print('hello-sandbox')"],
                workspace=ws,
                timeout_seconds=30,
            )
        )
        assert result.exit_code == 0
        assert "hello-sandbox" in result.stdout
        assert result.isolation_level.value in ("container", "linux_namespace", "process")
    finally:
        cleanup_workspace(ws)


def test_timeout_is_enforced() -> None:
    ws = make_workspace(prefix="unit")
    try:
        result = execute(
            ExecutionRequest(
                argv=[sys.executable, "-c", "import time; time.sleep(30)"],
                workspace=ws,
                timeout_seconds=1,
            )
        )
        assert result.timed_out or (result.exit_code not in (0, None))
    finally:
        cleanup_workspace(ws)


def test_inherited_secrets_do_not_reach_the_child(monkeypatch) -> None:
    monkeypatch.setenv("GITLAB_TOKEN", "child-must-not-see-1")
    ws = make_workspace(prefix="unit")
    try:
        result = execute(
            ExecutionRequest(
                argv=[
                    sys.executable,
                    "-c",
                    "import os; print('TOKEN=' + repr(os.environ.get('GITLAB_TOKEN', 'ABSENT')))",
                ],
                workspace=ws,
                timeout_seconds=30,
            )
        )
        assert "TOKEN=''" in result.stdout
    finally:
        cleanup_workspace(ws)


def test_workspace_cleanup_removes_everything(tmp_path) -> None:
    ws = make_workspace(prefix="unit")
    assert ws.is_dir()
    (ws / "leftover.txt").write_text("x", encoding="utf-8")
    cleanup_workspace(ws)
    assert not ws.exists()


def test_probe_reports_only_real_capabilities() -> None:
    probe = probe_isolation()
    selected = probe["selected_isolation_level"]
    assert selected in ("container", "linux_namespace", "process")
    # The probe must describe what exists, not what we wish existed.
    if selected == "container":
        assert probe["container"]["available"] is True
    if selected == "linux_namespace":
        assert probe["linux_namespace"]["available"] is True


def test_resolve_matches_probe() -> None:
    assert resolve_isolation().value == probe_isolation()["selected_isolation_level"]


@pytest.mark.skipif(
    resolve_isolation().value != "linux_namespace",
    reason="network denial is only measurable under the namespace tier",
)
def test_network_is_disabled_under_namespace_tier() -> None:
    ws = make_workspace(prefix="unit")
    try:
        result = execute(
            ExecutionRequest(
                argv=[
                    sys.executable,
                    "-c",
                    "import socket\n"
                    "try:\n"
                    "    socket.create_connection(('1.1.1.1', 53), timeout=2)\n"
                    "    print('NET=UP')\n"
                    "except Exception:\n"
                    "    print('NET=DOWN')",
                ],
                workspace=ws,
                timeout_seconds=30,
                allow_network=False,
            )
        )
        assert "NET=DOWN" in result.stdout
        assert result.network_disabled is True
    finally:
        cleanup_workspace(ws)
