"""Executor interface + local subprocess sandbox backend."""
from __future__ import annotations

import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Protocol


@dataclass
class ExecResult:
    ok: bool
    stdout: str
    stderr: str
    wall_ms: int
    exit_code: int


class Executor(Protocol):
    def run(self, code: str, timeout_s: int = 10) -> ExecResult: ...


def _scrub_env() -> dict:
    """Minimal env for the sandboxed child: no secrets, no parent vars."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "PYTHONHASHSEED": "0",
    }


_NO_CONFINEMENT_WARNING = (
    "WARNING: no filesystem confinement available (install bwrap/proot)")

_NO_MEM_RLIMIT_WARNING = (
    "WARNING: memory rlimit disabled on Android/bionic (linker CFI "
    "conflict); CPU/file limits active")

_IS_ANDROID = (
    ("ANDROID_ROOT" in os.environ)
    or ("com.termux" in os.environ.get("PREFIX", ""))
    or (hasattr(os, "uname") and "android" in os.uname().version.lower())
)


def _detect_backend() -> str:
    """Best-available filesystem confinement backend for this host."""
    if shutil.which("bwrap"):
        return "bwrap"
    if shutil.which("proot"):
        return "proot"
    return "none"


class LocalSubprocessExecutor:
    """Runs ``python -I -c <code>`` in a fresh temp dir with rlimits.

    - resource.setrlimit for AS (address space), CPU, NOFILE, FSIZE
    - scrubbed environment (no parent env vars beyond a minimal allowlist)
    - killed (whole process group) on timeout
    - temp dir always deleted afterwards
    - filesystem confinement, best available on the host:
        * ``bwrap`` (bubblewrap): temp dir is the only writable mount,
          tmpfs on /tmp, read-only binds of /usr /lib /lib64 /bin /sbin /etc;
        * ``proot`` (Termux): ``-b <tempdir>:/workspace -w /workspace`` plus
          a scratch dir bound over /tmp;
        * otherwise: no confinement. The child CAN write outside its temp
          dir; ExecResult.stderr carries an explicit WARNING line. For
          production use a cloud adapter (E2B/Modal) instead.
    """

    def __init__(self, mem_mb: int = 256, cpu_s: int = 5) -> None:
        self.mem_mb = mem_mb
        self.cpu_s = cpu_s
        self.backend = _detect_backend()

    def _confined_argv(self, tmpdir: str, code: str,
                       scratch: str | None) -> list[str]:
        """Wrap the python invocation with the best-available confinement."""
        base = [sys.executable, "-I", "-c", code]
        if self.backend == "bwrap":
            argv = ["bwrap", "--die-with-parent",
                    "--dev", "/dev", "--proc", "/proc",
                    "--tmpfs", "/tmp",
                    "--bind", tmpdir, "/workspace",
                    "--chdir", "/workspace"]
            for d in ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc"):
                if os.path.isdir(d):
                    argv += ["--ro-bind", d, d]
            return argv + ["--"] + base
        if self.backend == "proot":
            argv = ["proot", "-b", f"{tmpdir}:/workspace"]
            if scratch:  # shadow /tmp so writes there stay inside scratch
                argv += ["-b", f"{scratch}:/tmp"]
            return argv + ["-w", "/workspace"] + base
        return base

    def _apply_limits(self) -> None:  # runs in child via preexec_fn
        if not _IS_ANDROID:
            # RLIMIT_AS breaks bionic's linker CFI shadow mapping: the child
            # aborts in the linker before any python code runs. Skip AS (and
            # DATA) on Android; CPU/file limits still apply.
            mem = self.mem_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_CPU, (self.cpu_s, self.cpu_s))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))

    def run(self, code: str, timeout_s: int = 10) -> ExecResult:
        tmpdir = tempfile.mkdtemp(prefix="sicqg_sandbox_")
        scratch = (tempfile.mkdtemp(prefix="sicqg_tmp_")
                   if self.backend == "proot" else None)
        start = time.monotonic()
        proc = None
        try:
            proc = subprocess.Popen(
                self._confined_argv(tmpdir, code, scratch),
                cwd=tmpdir,
                env=_scrub_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                preexec_fn=self._apply_limits,
                start_new_session=True,  # own process group -> killable
            )
            try:
                out, err = proc.communicate(timeout=timeout_s)
                wall = int((time.monotonic() - start) * 1000)
                code_rc = proc.returncode if proc.returncode is not None else -1
                if self.backend == "none":
                    err = (err or "") + "\n[sandbox] " + _NO_CONFINEMENT_WARNING
                if _IS_ANDROID:
                    err = (err or "") + "\n[sandbox] " + _NO_MEM_RLIMIT_WARNING
                return ExecResult(
                    ok=proc.returncode == 0,
                    stdout=out,
                    stderr=err,
                    wall_ms=wall,
                    exit_code=code_rc,
                )
            except subprocess.TimeoutExpired:
                try:
                    import signal
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                out, err = proc.communicate()
                wall = int((time.monotonic() - start) * 1000)
                tail = "\n[sandbox] timeout: killed"
                if self.backend == "none":
                    tail += "\n[sandbox] " + _NO_CONFINEMENT_WARNING
                if _IS_ANDROID:
                    tail += "\n[sandbox] " + _NO_MEM_RLIMIT_WARNING
                return ExecResult(
                    ok=False,
                    stdout=out or "",
                    stderr=(err or "") + tail,
                    wall_ms=wall,
                    exit_code=-9,
                )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            if scratch:
                shutil.rmtree(scratch, ignore_errors=True)


class E2BExecutor:
    """Stub for an E2B (e2b.dev) cloud sandbox executor.

    Setup (not part of this offline package):
      pip install e2b
      export E2B_API_KEY=...   # from https://e2b.dev dashboard
    Then implement run() via e2b.Sandbox to execute code remotely.
    """

    def run(self, code: str, timeout_s: int = 10) -> ExecResult:
        raise NotImplementedError(
            "E2BExecutor requires the 'e2b' package and E2B_API_KEY; see class docstring."
        )


class ModalExecutor:
    """Stub for a Modal (modal.com) cloud sandbox executor.

    Setup (not part of this offline package):
      pip install modal
      modal token new   # authenticate
    Then implement run() via modal.Sandbox to execute code remotely.
    """

    def run(self, code: str, timeout_s: int = 10) -> ExecResult:
        raise NotImplementedError(
            "ModalExecutor requires the 'modal' package and modal auth; see class docstring."
        )
