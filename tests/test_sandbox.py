import os
import time

import pytest

from sicqg_triad.sandbox import (
    _IS_ANDROID,
    E2BExecutor,
    ExecResult,
    LocalSubprocessExecutor,
    ModalExecutor,
)


def test_basic_success():
    r = LocalSubprocessExecutor().run("print('hello')")
    assert r.ok and r.stdout.strip() == "hello"
    assert r.exit_code == 0 and r.wall_ms >= 0


def test_nonzero_exit_not_ok():
    r = LocalSubprocessExecutor().run("import sys; sys.exit(3)")
    assert not r.ok and r.exit_code == 3


def test_timeout_kills_infinite_loop():
    ex = LocalSubprocessExecutor(cpu_s=60)
    start = time.monotonic()
    r = ex.run("while True: pass", timeout_s=1)
    elapsed = time.monotonic() - start
    assert not r.ok
    assert r.exit_code != 0
    assert elapsed < 10  # killed promptly, not left running


@pytest.mark.skipif(_IS_ANDROID, reason="memory rlimit disabled on Android/bionic")
def test_memory_limit_fails_large_allocation():
    ex = LocalSubprocessExecutor(mem_mb=64)
    r = ex.run("x = bytearray(512 * 1024 * 1024); print(len(x))")
    assert not r.ok  # MemoryError / killed under RLIMIT_AS


@pytest.mark.skipif(not _IS_ANDROID, reason="Android-only memory rlimit warning")
def test_android_memory_rlimit_warning_in_stderr():
    r = LocalSubprocessExecutor().run("print('hi')")
    assert r.ok
    assert ("memory rlimit disabled on Android/bionic "
            "(linker CFI conflict); CPU/file limits active") in r.stderr


def test_temp_dir_removed():
    ex = LocalSubprocessExecutor()
    marker = {}
    r = ex.run("import os; print(os.getcwd())")
    assert r.ok
    tmp = r.stdout.strip()
    assert not os.path.exists(tmp), f"temp dir {tmp} was not removed"


def test_parent_env_not_visible():
    secret = "topsecret123"
    os.environ["SICQG_TEST_SECRET"] = secret
    try:
        r = LocalSubprocessExecutor().run(
            "import os; print(repr(dict(os.environ)))"
        )
        assert r.ok
        assert secret not in r.stdout
        assert "SICQG_TEST_SECRET" not in r.stdout
    finally:
        del os.environ["SICQG_TEST_SECRET"]


def test_isolated_mode_no_user_site():
    # python -I implies -s: user site-packages must not leak in
    r = LocalSubprocessExecutor().run(
        "import site; print(site.ENABLE_USER_SITE)"
    )
    assert r.ok and r.stdout.strip() == "False"


def test_cloud_stubs_raise_notimplemented():
    for cls in (E2BExecutor, ModalExecutor):
        with pytest.raises(NotImplementedError):
            cls().run("print(1)")


def test_no_write_outside_temp_dir_or_honest_warning():
    ex = LocalSubprocessExecutor()
    target = "/tmp/sicqg_escape_probe.txt"
    if os.path.exists(target):
        os.unlink(target)
    code = (
        "import os\n"
        f"open({target!r}, 'w').write('escaped')\n"
        "print(os.getcwd())"
    )
    try:
        r = ex.run(code)
        assert r.stdout.strip()  # ran and reported its cwd
        cwd = r.stdout.strip().splitlines()[-1]
        if ex.backend in ("bwrap", "proot"):
            # confined: the escape file must not exist on the real fs
            assert not os.path.exists(target), (
                f"{ex.backend} confinement failed: wrote outside temp dir")
            assert "WARNING: no filesystem confinement" not in r.stderr
        else:
            # unconfined: must be honest about it
            assert "WARNING: no filesystem confinement available" in r.stderr
    finally:
        if os.path.exists(target):
            os.unlink(target)
