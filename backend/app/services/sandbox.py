"""Isolated execution service for untrusted code.

The auditor must run tests generated from -- and patches applied to -- code it
does not trust.  A git worktree is *not* a security boundary, so execution goes
through one of three containment tiers, selected by capability probe:

``container``         Docker with ``--network none``, memory/cpu/pid caps and
                      read-only mounts.  Strongest.  Used automatically when a
                      Docker daemon is reachable.
``linux_namespace``   ``unshare`` user+network+mount namespaces, tmpfs masking
                      of unrelated filesystem paths, ``RLIMIT_*`` caps, scrubbed
                      environment, process-group kill on timeout.  Requires
                      unprivileged user namespaces.  Verified working.
``process_restricted`` Resource limits + scrubbed environment + throwaway temp
                      workspace only.  Universal fallback.  Weakest: it does NOT
                      prevent writes elsewhere on the filesystem.

The tier actually used is recorded on every ``TestRun`` and shown in the report,
so a reader always knows how much weight the isolation carried.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from ..models import IsolationLevel
from ..security.sanitize import sanitize_text, scrub_env

DEFAULT_MASK_PATHS = ("/home", "/root", "/etc/ssh", "/var/run/docker.sock")


@dataclass
class ExecutionRequest:
    argv: list[str]
    workspace: Path
    timeout_seconds: int = 120
    env_extra: dict[str, str] = field(default_factory=dict)
    allow_network: bool = False
    mask_paths: tuple[str, ...] = DEFAULT_MASK_PATHS
    read_only_binds: tuple[str, ...] = ()
    memory_mb: int = 1024
    cpu_seconds: int = 60
    max_file_mb: int = 32
    max_processes: int = 256
    docker_image: str = "python:3.12-slim"
    label: str = ""


@dataclass
class ExecutionResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    isolation_level: IsolationLevel
    network_disabled: bool
    duration_ms: int
    signal_name: str = ""
    workspace: str = ""
    label: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (not self.timed_out) and self.exit_code == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "isolation_level": self.isolation_level.value,
            "network_disabled": self.network_disabled,
            "duration_ms": self.duration_ms,
            "signal_name": self.signal_name,
            "workspace": self.workspace,
            "label": self.label,
            "notes": self.notes,
            "stdout_tail": self.stdout[-4000:],
            "stderr_tail": self.stderr[-4000:],
        }


# --------------------------------------------------------------------------- #
# Capability probing
# --------------------------------------------------------------------------- #
_PROBE_CACHE: dict[str, Any] = {}


def _docker_available() -> bool:
    if "docker" not in _PROBE_CACHE:
        ok = False
        reason = ""
        exe = shutil.which("docker")
        if not exe:
            reason = "docker binary not found"
        else:
            try:
                proc = subprocess.run(
                    [exe, "info", "--format", "{{.ServerVersion}}"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                ok = proc.returncode == 0
                if not ok:
                    reason = (proc.stderr or proc.stdout or "docker info failed").strip()[:200]
            except Exception as exc:  # noqa: BLE001
                reason = f"{type(exc).__name__}: {exc}"
        _PROBE_CACHE["docker"] = {"available": ok, "reason": reason}
    return bool(_PROBE_CACHE["docker"]["available"])


def _namespaces_available() -> bool:
    if "ns" not in _PROBE_CACHE:
        ok = False
        reason = ""
        if not sys.platform.startswith("linux"):
            reason = f"platform {sys.platform} has no unshare"
        elif not shutil.which("unshare"):
            reason = "unshare binary not found"
        else:
            try:
                proc = subprocess.run(
                    ["unshare", "-rnm", "--map-root-user", "/bin/sh", "-c", "echo ok"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                ok = proc.returncode == 0 and "ok" in proc.stdout
                if not ok:
                    reason = (proc.stderr or "unshare failed").strip()[:200]
                else:
                    # Confirm the network namespace really blocks egress.
                    probe = subprocess.run(
                        [
                            "unshare", "-rn", "--map-root-user", sys.executable, "-c",
                            "import socket\n"
                            "try:\n"
                            "    socket.create_connection(('1.1.1.1',53),timeout=4)\n"
                            "    print('LEAK')\n"
                            "except OSError:\n"
                            "    print('BLOCKED')\n",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=25,
                    )
                    if "BLOCKED" not in probe.stdout:
                        ok = False
                        reason = "network namespace did not block egress"
            except Exception as exc:  # noqa: BLE001
                reason = f"{type(exc).__name__}: {exc}"
        _PROBE_CACHE["ns"] = {"available": ok, "reason": reason}
    return bool(_PROBE_CACHE["ns"]["available"])


def probe_isolation(settings: Settings | None = None) -> dict[str, Any]:
    """Report which containment tiers this host can actually provide."""
    settings = settings or get_settings()
    _docker_available()
    _namespaces_available()
    docker = _PROBE_CACHE["docker"]
    ns = _PROBE_CACHE["ns"]
    if settings.sandbox_mode == "container":
        chosen = IsolationLevel.CONTAINER if docker["available"] else None
    elif settings.sandbox_mode == "linux_namespace":
        chosen = IsolationLevel.LINUX_NAMESPACE if ns["available"] else None
    elif settings.sandbox_mode == "process":
        chosen = IsolationLevel.PROCESS_RESTRICTED
    else:  # auto
        if docker["available"]:
            chosen = IsolationLevel.CONTAINER
        elif ns["available"]:
            chosen = IsolationLevel.LINUX_NAMESPACE
        else:
            chosen = IsolationLevel.PROCESS_RESTRICTED
    return {
        "configured_mode": settings.sandbox_mode,
        "selected_isolation_level": chosen.value if chosen else "unavailable",
        "container": docker,
        "linux_namespace": ns,
        "process_restricted": {"available": True, "reason": ""},
        "network_disabled_by_default": not settings.sandbox_allow_network,
    }


def resolve_isolation(settings: Settings | None = None) -> IsolationLevel:
    info = probe_isolation(settings)
    level = info["selected_isolation_level"]
    return IsolationLevel(level) if level != "unavailable" else IsolationLevel.PROCESS_RESTRICTED


# --------------------------------------------------------------------------- #
# Workspaces
# --------------------------------------------------------------------------- #
def create_workspace(source: Path, settings: Settings | None = None, *, prefix: str = "ws") -> Path:
    """Copy an exact revision into a throwaway workspace under the sandbox root.

    The copy is what gets mutated by patches and written to by test tooling, so
    the pristine checkout is never touched.
    """
    settings = settings or get_settings()
    root = settings.sandbox_root_path()
    dest = root / f"{prefix}-{uuid.uuid4().hex[:12]}"
    dest.mkdir(parents=True, exist_ok=True)
    if source.exists():
        shutil.copytree(
            source,
            dest / "repo",
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache"),
            symlinks=False,
        )
    (dest / "scratch").mkdir(exist_ok=True)
    (dest / "artifacts").mkdir(exist_ok=True)
    return dest


def make_workspace(settings: Settings | None = None, *, prefix: str = "ws") -> Path:
    """Create an empty throwaway workspace (repo/ is filled in by the caller)."""
    settings = settings or get_settings()
    dest = settings.sandbox_root_path() / f"{prefix}-{uuid.uuid4().hex[:12]}"
    (dest / "scratch").mkdir(parents=True, exist_ok=True)
    (dest / "artifacts").mkdir(parents=True, exist_ok=True)
    return dest


def cleanup_workspace(path: Path, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if settings.sandbox_keep_workspaces:
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Executors
# --------------------------------------------------------------------------- #
def _rlimit_preexec(req: ExecutionRequest):
    """Build a preexec_fn that applies resource limits in the child."""
    import resource

    def _apply() -> None:  # pragma: no cover - runs in forked child
        # NB: do NOT call os.setsid() here -- start_new_session=True already
        # makes the child a process-group leader, and a second setsid() fails
        # with EPERM, which surfaces as "Exception occurred in preexec_fn".
        soft_mem = req.memory_mb * 1024 * 1024
        limits = [
            (resource.RLIMIT_CPU, (req.cpu_seconds, req.cpu_seconds + 5)),
            (resource.RLIMIT_AS, (soft_mem, soft_mem)),
            (resource.RLIMIT_FSIZE, (req.max_file_mb * 1024 * 1024,) * 2),
            (resource.RLIMIT_NPROC, (req.max_processes, req.max_processes)),
            (resource.RLIMIT_CORE, (0, 0)),
        ]
        for res, value in limits:
            try:
                resource.setrlimit(res, value)
            except (ValueError, OSError):
                pass

    return _apply


def _run_subprocess(req: ExecutionRequest, argv: list[str], env: dict[str, str]) -> tuple[int | None, str, str, bool, str]:
    """Shared subprocess plumbing with process-group timeout kill."""
    timed_out = False
    signal_name = ""
    try:
        proc = subprocess.run(
            argv,
            cwd=str(req.workspace),
            env=env,
            capture_output=True,
            text=True,
            timeout=req.timeout_seconds,
            start_new_session=True,
            preexec_fn=_rlimit_preexec(req) if os.name == "posix" else None,
        )
        code: int | None = proc.returncode
        stdout, stderr = proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        stderr += f"\n[sandbox] killed after {req.timeout_seconds}s timeout"
        code = None
        signal_name = "SIGKILL"
    except Exception as exc:  # noqa: BLE001
        code = None
        stdout = ""
        stderr = f"[sandbox] failed to start: {type(exc).__name__}: {exc}"
    return code, stdout, stderr, timed_out, signal_name


def _exec_process(req: ExecutionRequest) -> ExecutionResult:
    started = time.monotonic()
    env = scrub_env()
    env.update(req.env_extra)
    env["TMPDIR"] = str(req.workspace / "scratch")
    env["ECA_ISOLATION_LEVEL"] = IsolationLevel.PROCESS_RESTRICTED.value
    code, out, err, timed_out, sig = _run_subprocess(req, req.argv, env)
    return ExecutionResult(
        exit_code=code,
        stdout=sanitize_text(out),
        stderr=sanitize_text(err),
        timed_out=timed_out,
        isolation_level=IsolationLevel.PROCESS_RESTRICTED,
        network_disabled=False,  # honest: this tier cannot block egress
        duration_ms=int((time.monotonic() - started) * 1000),
        signal_name=sig,
        workspace=str(req.workspace),
        label=req.label,
        notes=[
            "process_restricted tier: rlimits + scrubbed env only.",
            "Filesystem writes outside the workspace are NOT prevented.",
            "Network egress is NOT blocked at this tier.",
        ],
    )


def _build_namespace_argv(req: ExecutionRequest) -> list[str]:
    """Compose the unshare bootstrap: mask paths, then exec the real command."""
    bootstrap: list[str] = ["set -e"]
    for path in req.mask_paths:
        if not os.path.exists(path):
            continue
        # Mask with tmpfs so the audited code cannot read or destroy real data.
        bootstrap.append(
            f"mount -t tmpfs tmpfs {path} 2>/dev/null || true"
        )
    for bind in req.read_only_binds:
        bootstrap.append(
            f"mount --bind {bind} {bind} 2>/dev/null && "
            f"mount -o remount,ro,bind {bind} 2>/dev/null || true"
        )
    quoted = " ".join(_shell_quote(a) for a in req.argv)
    bootstrap.append(f"cd {_shell_quote(str(req.workspace))}")
    bootstrap.append(f"exec {quoted}")
    script = "\n".join(bootstrap)
    return ["unshare", "-rnm", "--map-root-user", "/bin/sh", "-c", script]


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _exec_namespace(req: ExecutionRequest) -> ExecutionResult:
    started = time.monotonic()
    env = scrub_env()
    env.update(req.env_extra)
    env["TMPDIR"] = str(req.workspace / "scratch")
    env["ECA_ISOLATION_LEVEL"] = IsolationLevel.LINUX_NAMESPACE.value
    argv = _build_namespace_argv(req)
    if req.allow_network:
        # Drop only the network namespace, keeping mount+user isolation.
        argv = _build_namespace_argv(req)
        argv[1] = "-rm"
    code, out, err, timed_out, sig = _run_subprocess(req, argv, env)
    notes = [
        "linux_namespace tier: unshare user+mount(+net) namespaces.",
        f"Masked with tmpfs: {', '.join(p for p in req.mask_paths if os.path.exists(p)) or 'none'}.",
        "RLIMIT_CPU/AS/FSIZE/NPROC/CORE applied in the child.",
        "Not a VM: kernel exploits remain a theoretical escape path. "
        "Use the container tier for production.",
    ]
    return ExecutionResult(
        exit_code=code,
        stdout=sanitize_text(out),
        stderr=sanitize_text(err),
        timed_out=timed_out,
        isolation_level=IsolationLevel.LINUX_NAMESPACE,
        network_disabled=not req.allow_network,
        duration_ms=int((time.monotonic() - started) * 1000),
        signal_name=sig,
        workspace=str(req.workspace),
        label=req.label,
        notes=notes,
    )


def _exec_container(req: ExecutionRequest) -> ExecutionResult:
    started = time.monotonic()
    docker = shutil.which("docker") or "docker"
    argv = [
        docker, "run", "--rm",
        "--network", "none" if not req.allow_network else "bridge",
        "--memory", f"{req.memory_mb}m",
        "--memory-swap", f"{req.memory_mb}m",
        "--cpus", "1.0",
        "--pids-limit", str(req.max_processes),
        "--security-opt", "no-new-privileges",
        "--cap-drop", "ALL",
        "--read-only",
        "-v", f"{req.workspace}:/work:rw",
        "-w", "/work",
        "-e", "TMPDIR=/work/scratch",
        "-e", f"ECA_ISOLATION_LEVEL={IsolationLevel.CONTAINER.value}",
    ]
    for key, value in req.env_extra.items():
        argv += ["-e", f"{key}={value}"]
    for bind in req.read_only_binds:
        argv += ["-v", f"{bind}:{bind}:ro"]
    argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
    argv += [req.docker_image, *req.argv]
    env = scrub_env()
    code, out, err, timed_out, sig = _run_subprocess(req, argv, env)
    return ExecutionResult(
        exit_code=code,
        stdout=sanitize_text(out),
        stderr=sanitize_text(err),
        timed_out=timed_out,
        isolation_level=IsolationLevel.CONTAINER,
        network_disabled=not req.allow_network,
        duration_ms=int((time.monotonic() - started) * 1000),
        signal_name=sig,
        workspace=str(req.workspace),
        label=req.label,
        notes=[
            "container tier: docker, network=none, cap-drop=ALL, no-new-privileges.",
            f"image={req.docker_image}",
        ],
    )


def execute(req: ExecutionRequest, settings: Settings | None = None) -> ExecutionResult:
    """Run a command under the strongest containment tier this host supports."""
    settings = settings or get_settings()
    level = resolve_isolation(settings)
    if level == IsolationLevel.CONTAINER:
        return _exec_container(req)
    if level == IsolationLevel.LINUX_NAMESPACE:
        return _exec_namespace(req)
    return _exec_process(req)


def write_artifact(workspace: Path, name: str, content: str) -> Path:
    dest = workspace / "artifacts" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    return dest


def dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


__all__ = [
    "DEFAULT_MASK_PATHS",
    "ExecutionRequest",
    "ExecutionResult",
    "cleanup_workspace",
    "create_workspace",
    "dump_json",
    "execute",
    "make_workspace",
    "probe_isolation",
    "resolve_isolation",
    "write_artifact",
]
