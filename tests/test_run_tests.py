"""Check ./run-tests and the pytest guard through their command lines, from inside the sandbox they build."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUN_TESTS = ROOT / "run-tests"
MARKER = "STAYMUX_TEST_SANDBOX"


def outside_env(**changes):
    """The environment a caller outside the sandbox has: without the marker, so nothing here can recurse into the
    whole suite, plus the given changes."""
    env = {name: value for name, value in os.environ.items() if name != MARKER}
    env.update(changes)
    return env


def run_tests(*arguments, env=None, python=sys.executable):
    return subprocess.run(
        [str(python), str(RUN_TESTS), *arguments],
        env=outside_env() if env is None else env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_a_command_after_a_double_dash_runs_in_the_sandbox_with_its_own_exit_status():
    result = run_tests("--", "sh", "-c", 'echo "marker=$STAYMUX_TEST_SANDBOX"; exit 3')
    assert (result.returncode, result.stdout) == (3, "marker=1\n"), result.stderr


def test_the_sandbox_hides_your_sessions_services_keeps_the_checkout_read_only_and_gives_an_empty_home():
    # Each variable leads a forgetful test to a service of yours, such as TMUX_TMPDIR to your own tmux server.
    leads = {
        "TMUX_TMPDIR": "/home/someone/.tmux",
        "TMUX": "/tmp/tmux-1000/default,1,0",
        "SSH_AUTH_SOCK": "/run/user/1000/ssh-agent",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "XDG_RUNTIME_DIR": "/run/user/1000",
    }
    probe = (
        'test -w "$PWD" && echo checkout-writable; '
        'test -w "$HOME" && test -z "$(ls -A "$HOME")" && echo empty-home-writable; '
        'echo "run=$(ls -A /run | wc -l)"; '
        "env"
    )
    result = run_tests("--", "sh", "-c", probe, env=outside_env(**leads))
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[:2] == ["empty-home-writable", "run=0"]
    assert [line for line in lines[2:] if line.partition("=")[0] in leads] == []


def test_a_python_environment_under_tmp_stays_visible_in_the_sandbox():
    # The sandbox hides /tmp, where a contributor may keep the virtual environment that runs the tests. It shows the
    # temp folder again, so the environment goes straight into /tmp, outside it.
    folder = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        venv = folder / "venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", "--system-site-packages", str(venv)], check=True)
        probe = 'test -x "$1/bin/python3" && echo visible'
        result = run_tests("--", "sh", "-c", probe, "sh", str(venv), python=venv / "bin/python3")
    finally:
        shutil.rmtree(folder)
    assert (result.returncode, result.stdout) == (0, "visible\n"), result.stderr


def test_a_python_user_folder_under_tmp_that_doesnt_exist_yet_doesnt_stop_the_sandbox(tmp_path):
    # As with a fresh HOME under /tmp, whose ~/.local nobody has made.
    result = run_tests("--", "sh", "-c", "echo ran", env=outside_env(PYTHONUSERBASE=str(tmp_path / "missing")))
    assert (result.returncode, result.stdout) == (0, "ran\n"), result.stderr


def test_a_python_import_path_under_tmp_stays_visible_in_the_sandbox():
    # Such as PYTHONPATH naming a folder of test dependencies there; outside the temp folder, which is shown anyway.
    folder = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        (folder / "dependency.py").write_text("FOUND = 'found'\n")
        probe = ["python3", "-c", "import dependency; print(dependency.FOUND)"]
        result = run_tests("--", *probe, env=outside_env(PYTHONPATH=str(folder)))
    finally:
        shutil.rmtree(folder)
    assert (result.returncode, result.stdout) == (0, "found\n"), result.stderr


def fake_bwrap(tmp_path, script):
    """A directory holding a stand-in bwrap, to put first on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "bwrap").write_text(f"#!/bin/sh\n{script}\n")
    (bin_dir / "bwrap").chmod(0o755)
    return f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


def test_nothing_runs_without_bubblewrap(tmp_path):
    result = run_tests("--", "sh", "-c", "echo ran", env=outside_env(PATH=str(tmp_path)))
    assert (result.returncode, result.stdout) == (2, "")
    assert "not running the tests: bubblewrap (bwrap) is not installed" in result.stderr


def test_nothing_runs_when_bubblewrap_cant_build_the_sandbox(tmp_path):
    path = fake_bwrap(tmp_path, "echo 'bwrap: setting up uid map: Permission denied' >&2; exit 1")
    result = run_tests("--", "sh", "-c", "echo ran", env=outside_env(PATH=path))
    assert (result.returncode, result.stdout) == (2, "")
    assert "bwrap could not build the sandbox: bwrap: setting up uid map: Permission denied" in result.stderr


def test_nothing_runs_when_the_sandbox_leaves_your_folders_writable(tmp_path):
    # This bwrap ignores every option and runs the command as it is, so the probe finds HOME writable.
    path = fake_bwrap(tmp_path, 'while [ "$1" != -- ]; do shift; done; shift; exec "$@"')
    result = run_tests("--", "sh", "-c", "echo ran", env=outside_env(PATH=path))
    assert (result.returncode, result.stdout) == (2, "")
    assert f"the sandbox leaves writable: {Path.home()}" in result.stderr


def checkout_with(tmp_path, files):
    """A checkout holding this one's pytest settings and guard, and the given files, by path."""
    checkout = tmp_path / "checkout"
    (checkout / "tests").mkdir(parents=True)
    for name in ("conftest.py", "pytest.ini"):
        shutil.copy(ROOT / name, checkout / name)
    for path, text in files.items():
        (checkout / path).write_text(text)
    return checkout


def pytest_in(folder, *arguments, env):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *arguments],
        cwd=folder,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result.returncode, result.stdout + result.stderr


PASSING = "def test_ran():\n    pass\n"


@pytest.mark.parametrize(
    ("folder", "arguments"),
    [(".", []), (".", ["tests/test_one.py"]), ("tests", ["test_one.py"]), (".", ["test_at_the_root.py"])],
    ids=["everything", "one-file", "from-tests", "a-test-at-the-root"],
)
def test_pytest_refuses_to_run_outside_the_sandbox(tmp_path, folder, arguments):
    checkout = checkout_with(tmp_path, {"tests/test_one.py": PASSING, "test_at_the_root.py": PASSING})
    status, output = pytest_in(checkout / folder, *arguments, env=outside_env())
    assert status == 4, output
    assert "run the tests with ./run-tests, which sandboxes them" in output


def test_pytest_refuses_before_any_other_conftest_runs(tmp_path):
    # A conftest.py further down may set up fixtures as it's imported; that must not happen outside the sandbox.
    sentinel = tmp_path / "ran"
    nested = f"from pathlib import Path\n\nPath({str(sentinel)!r}).touch()\n"
    checkout = checkout_with(tmp_path, {"tests/conftest.py": nested, "tests/test_one.py": PASSING})
    status, output = pytest_in(checkout, "tests/test_one.py", env=outside_env())
    assert (status, sentinel.exists()) == (4, False), output
    assert "run the tests with ./run-tests, which sandboxes them" in output


SKIPS = {
    "while-running": "import pytest\n\n\ndef test_skipped():\n    pytest.skip('cannot run here')\n",
    "while-collecting": "import pytest\n\npytest.skip('cannot run here', allow_module_level=True)\n",
}


@pytest.mark.parametrize("skip", SKIPS)
@pytest.mark.parametrize(("no_skips", "expected"), [("1", 1), (None, 0)], ids=["no-skips-asked", "skips-allowed"])
def test_a_skipped_test_fails_the_run_only_when_no_skips_are_asked_for(tmp_path, skip, no_skips, expected):
    # CI asks for none, so a test the runner can no longer run, such as a namespace test, can't pass unseen.
    checkout = checkout_with(tmp_path, {"tests/test_skip.py": SKIPS[skip], "tests/test_one.py": PASSING})
    env = {**os.environ, MARKER: "1"}
    env.pop("STAYMUX_NO_SKIPS", None)
    if no_skips:
        env["STAYMUX_NO_SKIPS"] = no_skips
    status, output = pytest_in(checkout, env=env)
    assert status == expected, output
    assert ("STAYMUX_NO_SKIPS=1" in output) == bool(no_skips), output
