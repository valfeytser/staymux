"""Check what staymux does before it starts or attaches, with systemd's commands stubbed on PATH."""

import os
import pty
import select
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "staymux"
BASH = shutil.which("bash") or "/bin/bash"  # found here, so a test may give staymux a bare PATH


def stubbed_env(tmp_path, **stubs):
    """Install each stub as a command named after its keyword (with dashes for underscores).

    Return an environment that finds them first on PATH.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, text in stubs.items():
        command = bin_dir / name.replace("_", "-")
        command.write_text(text)
        command.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def run_staymux(commands, env, timeout=10):
    """Source staymux, run the bash commands, and return the result and the seconds it took."""
    started = time.monotonic()
    result = subprocess.run(
        [BASH, "-c", f'source "$1"; {commands}', "staymux-test", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return result, time.monotonic() - started


# --- the initial PID namespace ------------------------------------------------


def test_the_initial_pid_namespace_is_the_one_the_kernel_always_numbers_the_same():
    # The namespace tests stand in for it, test_recovery.py's with the sandbox's own namespace and test_command.py's
    # with tests/host/readlink, so this is what checks the value holds_socket compares with: PROC_PID_INIT_INO in the
    # kernel's include/linux/proc_ns.h, the same from Linux 3.8 on, as /proc/PID/ns/pid names it.
    result, _ = run_staymux('printf %s "$INIT_PID_NS"', os.environ)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"pid:[{0xEFFFFFFC}]"


# --- the cgroup files --------------------------------------------------------


def test_staymux_reads_where_processes_run_from_the_kernel():
    # tests/host/staymux points CGROUP_FILES at files of its own, and in the sandbox's cgroup namespace the kernel's
    # files name no unit, as files that aren't there wouldn't, so no command test can tell /proc from a wrong folder.
    # This checks the value the command reads from.
    result, _ = run_staymux('printf %s "$CGROUP_FILES"', os.environ)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "/proc"


# --- waiting for a stopping staymux.slice --------------------------------------

# Stands in for `systemctl --user list-jobs [--no-legend] staymux.slice`: it lists a stop job of the slice
# for $STOP_US microseconds from its first call and, like systemctl, prints a legend unless told not to.
# The calls numbered in $FAIL cannot reach the manager, and those in $HANG never answer (leaving a mark).
STUB_SYSTEMCTL_JOBS = r"""#!/bin/bash
printf '%s\n' "$*" >>"$STUB_LOG"
call=$(wc -l <"$STUB_LOG")
now=${EPOCHREALTIME/./}
((call > 1)) || echo "$now" >"$STUB_LOG.start"
[[ " $FAIL " != *" $call "* ]] || { echo 'Failed to connect to bus: No such file or directory' >&2; exit 1; }
[[ " $HANG " != *" $call "* ]] || { : >"$STUB_LOG.hung"; exec sleep 60; }
args=" $* "
[[ $args == *' --user '* && $args == *' list-jobs '* && $args == *' staymux.slice '* ]] || exit 0
legend=1
[[ $args != *' --no-legend '* ]] || legend=0
if ((now < $(<"$STUB_LOG.start") + STOP_US)); then
    ((!legend)) || echo 'JOB UNIT          TYPE STATE'
    echo '  7 staymux.slice stop waiting'
    ((!legend)) || printf '\n1 jobs listed.\n'
elif ((legend)); then
    echo 'No jobs running.'
fi
"""


def wait_for_slice_stop(tmp_path, stop_for=0.0, fail="", hang="", give_up_after=None):
    """Run wait_for_slice_stop with a stop job listed for stop_for seconds.

    Return the result, the seconds it took, and whether a query hung.
    """
    env = stubbed_env(tmp_path, systemctl=STUB_SYSTEMCTL_JOBS)
    env.update(STUB_LOG=str(tmp_path / "systemctl.log"), STOP_US=str(int(stop_for * 1_000_000)), FAIL=fail, HANG=hang)
    shorter = f"SLICE_STOP_TIMEOUT={give_up_after}; " if give_up_after else ""
    result, took = run_staymux(f"{shorter}wait_for_slice_stop", env)
    return result, took, (tmp_path / "systemctl.log.hung").exists()


# "At once" allows for slow process start-up; a run that waited wrongly would take the full 30 s.


def test_wait_for_slice_stop_goes_on_at_once_when_no_stop_is_queued(tmp_path):
    result, took, _ = wait_for_slice_stop(tmp_path)
    assert (result.returncode, result.stderr) == (0, "")
    assert took < 5


def test_wait_for_slice_stop_waits_until_the_stop_has_finished(tmp_path):
    result, took, _ = wait_for_slice_stop(tmp_path, stop_for=1.5)
    assert result.returncode == 0, result.stderr
    assert result.stderr == "staymux: waiting for staymux.slice to finish stopping.\n"
    assert took >= 1.5


def test_wait_for_slice_stop_goes_on_when_the_manager_cannot_be_reached(tmp_path):
    # As before this check existed: without an answer there is no telling, and starting reports what is wrong.
    result, took, _ = wait_for_slice_stop(tmp_path, stop_for=60, fail="1")
    assert (result.returncode, result.stderr) == (0, "")
    assert took < 5


def test_wait_for_slice_stop_keeps_waiting_when_a_query_fails_during_the_stop(tmp_path):
    result, took, _ = wait_for_slice_stop(tmp_path, stop_for=1.5, fail="2")
    assert result.returncode == 0, result.stderr
    assert took >= 1.5


@pytest.mark.parametrize("hang", ["", "2"], ids=["queries-answer", "a-query-hangs"])
def test_wait_for_slice_stop_gives_up_after_its_timeout(tmp_path, hang):
    result, took, hung = wait_for_slice_stop(tmp_path, stop_for=60, hang=hang, give_up_after=1)
    assert result.returncode == 1
    assert "staymux: staymux.slice is still stopping after 1s." in result.stderr
    assert 1 <= took < 4
    assert hung == bool(hang)


def test_wait_for_slice_stop_bounds_a_first_query_that_hangs(tmp_path):
    # No answer before the time is up counts as no answer at all, so the run goes on, quietly.
    result, took, hung = wait_for_slice_stop(tmp_path, stop_for=60, hang="1", give_up_after=1)
    assert (result.returncode, result.stderr, hung) == (0, "", True)
    assert took < 4


# --- processes left in staymux.slice -------------------------------------------

LEFT_IN = "staymux-20261024-084700-4242.scope"
ALSO_IN = "tmux-spawn-0b5e0f4c-5d1e-4c1e-9d0e-7a7f2b8e1c42.scope"

# Stands in for `systemctl --user list-jobs --no-legend [PATTERN...]`: it runs $ON_QUERY, as the manager would go
# on stopping units meanwhile, then lists those of $STUB_JOBS, one "ID UNIT TYPE STATE" line each, whose unit
# matches a pattern given, or all with none given. Like systemctl, it highlights the unit and state of a running job
# when SYSTEMD_COLORS=1 forces colour, and, as systemd 239 (RHEL 8) does, shortens a long unit name to fit a $COLUMNS
# in the environment unless given --full. The calls numbered in $FAIL cannot reach the manager, and those in $HANG
# never answer (leaving a mark).
STUB_SYSTEMCTL_ALL_JOBS = r"""#!/bin/bash
printf '%s\n' "$*" >>"$STUB_LOG"
call=$(wc -l <"$STUB_LOG")
[[ " $FAIL " != *" $call "* ]] || { echo 'Failed to connect to bus: No such file or directory' >&2; exit 1; }
[[ " $HANG " != *" $call "* ]] || { : >"$STUB_LOG.hung"; exec sleep 60; }
[[ " $* " == *' --user '* && " $* " == *' list-jobs '* ]] || exit 0
patterns=() full=0
for arg; do
    [[ $arg != --full ]] || full=1
    [[ $arg == -* || $arg == list-jobs ]] || patterns+=("$arg")
done
eval "$ON_QUERY"
while read -r id unit type state; do
    [[ -n $id ]] || continue
    matched=$((${#patterns[@]} == 0))
    for pattern in "${patterns[@]}"; do [[ $unit != $pattern ]] || matched=1; done
    ((matched)) || continue
    ((full || ${COLUMNS:-999} >= 80 || ${#unit} <= 33)) || unit="${unit:0:11}…${unit: -20}"
    if [[ ${SYSTEMD_COLORS:-} == 1 && $state == running ]]; then
        printf '%s \e[0;1;39m%s\e[0m %s \e[0;1;39m%s\e[0m\n' "$id" "$unit" "$type" "$state"
    else
        printf '%s %s %s %s\n' "$id" "$unit" "$type" "$state"
    fi
done <<<"$STUB_JOBS"
"""


def report_leftovers(tmp_path, jobs="", on_query="", fail="", hang="", **env_extra):
    """Run report_leftovers, holding the lock, with a process left in each of two units of staymux.slice.

    The cgroup tree is a folder of the test's, $SLICE_DIR, and the user manager lists jobs and runs on_query as
    STUB_SYSTEMCTL_ALL_JOBS says; env_extra is added to the environment. Return the result, the seconds it took,
    and whether a query hung.
    """
    uid = os.getuid()
    slice_dir = tmp_path / "cgroup" / f"user.slice/user-{uid}.slice/user@{uid}.service/staymux.slice"
    for unit, pid in ((LEFT_IN, 4321), (ALSO_IN, 4322)):
        (slice_dir / unit).mkdir(parents=True)
        (slice_dir / unit / "cgroup.procs").write_text(f"{pid}\n")
    env = stubbed_env(tmp_path, systemctl=STUB_SYSTEMCTL_ALL_JOBS)
    env.update(STUB_LOG=str(tmp_path / "systemctl.log"), SLICE_DIR=str(slice_dir), STUB_JOBS=jobs, ON_QUERY=on_query)
    env.update(FAIL=fail, HANG=hang, **env_extra)
    result, took = run_staymux(f"CGROUP_ROOTS=({tmp_path / 'cgroup'}); JOBS_TIMEOUT=1; LOCKED=1; report_leftovers", env)
    return result, took, (tmp_path / "systemctl.log.hung").exists()


@pytest.mark.parametrize(
    ("jobs", "fail", "hang"),
    [("", "", ""), ("7 staymux.slice start waiting", "", ""), ("", "1", ""), ("", "", "1")],
    ids=["no-job-queued", "no-stop-queued", "manager-cannot-be-reached", "manager-does-not-answer"],
)
def test_report_leftovers_names_processes_an_earlier_server_left(tmp_path, jobs, fail, hang):
    result, took, hung = report_leftovers(tmp_path, jobs=jobs, fail=fail, hang=hang)
    assert result.returncode == 0, result.stderr
    assert "2 process(es) left by an earlier staymux server still run in staymux.slice." in result.stderr
    assert f"Stop them with: systemctl --user stop {LEFT_IN} {ALSO_IN}\n" in result.stderr, result.stderr
    # A manager that doesn't answer holds the lock only as long as JOBS_TIMEOUT.
    assert (hung, took < 4) == (bool(hang), True)


@pytest.mark.parametrize(
    ("stopping", "terminal"),
    [
        ("staymux.slice", {}),
        (f"{LEFT_IN} {ALSO_IN}", {}),
        (f"{LEFT_IN} {ALSO_IN}", {"SYSTEMD_COLORS": "1"}),
        (f"{LEFT_IN} {ALSO_IN}", {"COLUMNS": "40"}),
    ],
    ids=["the-slice", "each-unit-in-it", "colours-forced", "narrow-terminal"],
)
def test_report_leftovers_does_not_count_processes_that_a_stop_is_ending(tmp_path, stopping, terminal):
    # After `staymux -k all` inside a session, or `systemctl --user stop UNIT` as the warning advises, systemd takes
    # up to the units' stop timeout to end a pane that ignores SIGTERM and SIGHUP. Those processes are on their way
    # out, not left behind. A user's SYSTEMD_COLORS=1 would colour the units' names even in a pipe, and an exported
    # COLUMNS would shorten long ones on RHEL 8.
    # A slice's stop waits for its units' (as after `staymux -k all`); a unit's own runs.
    jobs = "\n".join(
        f"{n} {unit} stop {'waiting' if unit.endswith('.slice') else 'running'}"
        for n, unit in enumerate(stopping.split(), start=7)
    )
    result, _, _ = report_leftovers(tmp_path, jobs=jobs, **terminal)
    assert result.returncode == 0, result.stderr
    assert result.stderr == "staymux: 2 process(es) in staymux.slice are still being stopped.\n"


def test_report_leftovers_names_only_the_units_no_stop_is_ending(tmp_path):
    result, _, _ = report_leftovers(tmp_path, jobs=f"9 {ALSO_IN} stop running")
    assert result.returncode == 0, result.stderr
    assert "staymux: 1 process(es) in staymux.slice are still being stopped." in result.stderr, result.stderr
    assert "1 process(es) left by an earlier staymux server still run in staymux.slice." in result.stderr
    assert f"Stop them with: systemctl --user stop {LEFT_IN}\n" in result.stderr, result.stderr


def test_report_leftovers_says_nothing_of_a_stop_that_finished_while_it_asked(tmp_path):
    # The processes were read, then the stop finished before the manager answered: no job, and nothing left.
    result, _, _ = report_leftovers(tmp_path, on_query='for f in "$SLICE_DIR"/*/cgroup.procs; do : >"$f"; done')
    assert (result.returncode, result.stderr) == (0, "")


# --- lingering -----------------------------------------------------------------

# Stands in for logind's lingering setting, which loginctl show-user reports: on while $LINGER_FILE holds our UID.
STUB_LOGINCTL_SHOW_LINGER = r"""#!/bin/bash
[[ " $* " == *' show-user '* ]] || exit 0
[[ -e $LINGER_FILE && $(<"$LINGER_FILE") == "$EUID" ]] && echo yes || echo no
"""


def test_ensure_linger_names_a_missing_busctl(tmp_path):
    env = stubbed_env(tmp_path, loginctl=STUB_LOGINCTL_SHOW_LINGER)
    env.update(PATH=str(tmp_path / "bin"), LINGER_FILE=str(tmp_path / "linger"))  # nothing else, so no busctl
    result, _ = run_staymux("ME=someone; ensure_linger", env)
    assert result.returncode == 1
    assert "busctl (part of systemd) is missing" in result.stderr, result.stderr


# --- starting the server -------------------------------------------------------

# Stand in for systemd: systemd-run runs its command in place of a scope, systemctl logs what it is asked, and
# loginctl says lingering is on.
STUB_SYSTEMD_RUN = r"""#!/bin/bash
while [[ $# -gt 0 && $1 != tmux ]]; do shift; done
exec "$@"
"""
STUB_SYSTEMCTL_LOG = r"""#!/bin/bash
printf '%s\n' "$*" >>"$STUB_LOG"
"""
STUB_LOGINCTL_LINGER_ON = r"""#!/bin/bash
echo yes
"""


# The paths setup_environment would set, in the runtime directory a test gives.
PATHS = "SOCK=$XDG_RUNTIME_DIR/staymux.sock; STARTING=$XDG_RUNTIME_DIR/staymux.starting; "


def start_server(tmp_path, config, then=None, paths=PATHS):
    """Run start_server with START_TIMEOUT=1, a real tmux and this ~/.tmux.conf, on a socket of its own.

    Set the paths as in paths. Call then with the socket and the environment, if given, before listing the
    sessions. Return the result, the seconds it took, what systemctl was asked, and the sessions.
    """
    env = stubbed_env(
        tmp_path, systemd_run=STUB_SYSTEMD_RUN, systemctl=STUB_SYSTEMCTL_LOG, loginctl=STUB_LOGINCTL_LINGER_ON
    )
    home = tmp_path / "home"
    home.mkdir()
    (home / ".tmux.conf").write_text(config)
    sock_dir = Path(
        subprocess.run(["mktemp", "-d", "/tmp/staymux.XXXXXX"], capture_output=True, text=True).stdout.strip()
    )
    sock = sock_dir / "staymux.sock"
    env.update(
        HOME=str(home),
        STUB_LOG=str(tmp_path / "systemctl.log"),
        XDG_RUNTIME_DIR=str(sock_dir),
    )
    try:
        result, took = run_staymux(paths + "START_TIMEOUT=1; start_server first", env, timeout=15)
        if then:
            then(sock, env)
        try:  # a server stuck loading its config doesn't answer
            sessions = subprocess.run(
                ["tmux", "-S", str(sock), "list-sessions", "-F", "#{session_name}"],
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.split()
        except subprocess.TimeoutExpired:
            sessions = None
    finally:
        # Everything this server started names its socket, which no other test uses.
        subprocess.run(["pkill", "-KILL", "-f", str(sock)], check=False)
        subprocess.run(["pkill", "-KILL", "-f", "sleep 58.7"], check=False)  # the blocking config's job
        subprocess.run(["pkill", "-KILL", "-f", "sleep 2.7"], check=False)  # the slow config's job
    systemctl_log = tmp_path / "systemctl.log"
    return result, took, systemctl_log.read_text().splitlines() if systemctl_log.exists() else [], sessions


pytestmark_tmux = pytest.mark.skipif(not shutil.which("tmux"), reason="native tmux required")


@pytestmark_tmux
def test_start_server_stops_waiting_for_a_configuration_that_never_finishes_loading(tmp_path):
    # tmux keeps the client that starts the server waiting until its configuration has loaded, so a
    # run-shell that waits (a plugin manager cloning from the internet on an air-gapped host) held staymux,
    # and its lock, for as long. The config may already have started work (restored panes, say), so the
    # server is left running and the message says how to stop it.
    config = "run-shell \"trap '' TERM; exec sleep 58.7\"\n"
    result, took, systemctl, _ = start_server(tmp_path, config)
    assert result.returncode == 1
    assert "did not finish starting within 1s" in result.stderr, result.stderr
    assert "If it is stuck, stop it with: staymux -k all" in result.stderr, result.stderr
    assert "systemctl --user status staymux.slice" in result.stderr, result.stderr
    assert took < 6
    assert not any(" kill " in f" {line} " for line in systemctl), systemctl


@pytestmark_tmux
def test_start_server_lets_a_slow_configuration_finish_starting_after_it_stops_waiting(tmp_path):
    # Ending the client that started the server would cost the session it asked for, and with tmux 3.2 and
    # later the next client to connect before the config has loaded would load it again.
    loads = tmp_path / "loads"
    config = f'run-shell "echo loaded >>{loads}"\nrun-shell "sleep 2.7"\n'

    def another_client_meanwhile(sock, _):
        try:
            subprocess.run(["tmux", "-S", str(sock), "has-session"], capture_output=True, timeout=2)
        except subprocess.TimeoutExpired:
            pass
        time.sleep(3)  # the config's sleep has ended by now, so its client has created the session

    result, _, _, sessions = start_server(tmp_path, config, then=another_client_meanwhile)
    assert "did not finish starting within 1s" in result.stderr, result.stderr
    assert loads.read_text().splitlines() == ["loaded"]
    assert sessions == ["first"]


@pytestmark_tmux
def test_start_server_does_not_start_a_server_it_cannot_record_as_starting(tmp_path):
    # Without the record a later run would not wait for it, should it take longer than START_TIMEOUT.
    paths = PATHS.replace("$XDG_RUNTIME_DIR/staymux.starting", "$XDG_RUNTIME_DIR/missing/staymux.starting")
    result, _, _, sessions = start_server(tmp_path, "", paths=paths)
    assert result.returncode == 1
    assert "cannot record the start of the new tmux server" in result.stderr, result.stderr
    assert "could not start the tmux server" in result.stderr, result.stderr
    assert sessions == []


@pytestmark_tmux
def test_a_later_run_waits_for_a_server_that_an_earlier_run_is_still_starting(tmp_path):
    # A session created before the config has loaded misses what the config sets later, such as
    # default-command, so a later run waits for the start to finish before it opens one.
    waited = {}

    def later_run(_, env):
        result, took = run_staymux(PATHS + "START_TIMEOUT=5; wait_for_start", env, timeout=15)
        listed = subprocess.run(
            ["tmux", "-S", env["XDG_RUNTIME_DIR"] + "/staymux.sock", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        waited.update(result=result, took=took, sessions=listed.stdout.split())

    start_server(tmp_path, 'run-shell "sleep 2.7"\n', then=later_run)
    assert waited["result"].returncode == 0, waited["result"].stderr
    assert waited["sessions"] == ["first"]  # the earlier run's start has finished
    assert 0.5 < waited["took"] < 5


@pytestmark_tmux
def test_a_later_run_gives_up_on_a_server_that_never_finishes_starting(tmp_path):
    waited = {}

    def later_run(_, env):
        waited["result"], waited["took"] = run_staymux(PATHS + "START_TIMEOUT=1; wait_for_start", env, timeout=15)

    start_server(tmp_path, "run-shell \"trap '' TERM; exec sleep 58.7\"\n", then=later_run)
    result = waited["result"]
    assert result.returncode == 1
    assert "is still starting" in result.stderr, result.stderr
    assert "If it is stuck, stop it with: staymux -k all" in result.stderr, result.stderr
    assert waited["took"] < 5


def test_a_later_run_goes_on_at_once_when_no_start_is_pending(tmp_path):
    (tmp_path / "staymux.starting").write_text("999999999 1\n")  # a process that has gone
    result, took = run_staymux(PATHS + "wait_for_start", {**os.environ, "XDG_RUNTIME_DIR": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert took < 2
    assert not (tmp_path / "staymux.starting").exists()


# --- pausing after a warning ---------------------------------------------------


def pause_before_attaching(before, terminal, enter_after=None):
    """Run the bash commands in before, then pause_after_warnings, with PAUSE_SECONDS=2.

    On a pseudo-terminal if asked, pressing Enter after enter_after seconds if it is still waiting then.
    Return the exit status, what it printed, the seconds it took, and whether it was still waiting at Enter.
    """
    main, side = pty.openpty()
    started = time.monotonic()
    run = subprocess.Popen(
        [
            "bash",
            "-c",
            f'source "$1"; PAUSE_SECONDS=2; {before} 2>/dev/null; pause_after_warnings',
            "staymux-test",
            str(SCRIPT),
        ],
        stdin=side if terminal else subprocess.DEVNULL,
        stderr=side if terminal else subprocess.PIPE,
        stdout=subprocess.DEVNULL,
    )
    os.close(side)
    waiting_at_enter = None
    try:
        if enter_after is not None:
            time.sleep(enter_after)
            waiting_at_enter = run.poll() is None
            os.write(main, b"\n")
        run.wait(timeout=10)
        took = time.monotonic() - started
        shown = b""
        while select.select([main], [], [], 0)[0]:
            try:
                chunk = os.read(main, 4096)
            except OSError:
                break
            if not chunk:
                break
            shown += chunk
    finally:
        os.close(main)
        if run.poll() is None:
            run.kill()
    return run.returncode, shown.decode(errors="replace"), took, waiting_at_enter


@pytest.mark.parametrize(
    ("before", "terminal"),
    [(":", True), ("warn something", False), ("note just so you know", True)],
    ids=["no-warning", "no-terminal", "only-a-note"],
)
def test_pause_after_warnings_goes_on_at_once_without_a_warning_at_a_terminal(before, terminal):
    returncode, _, took, _ = pause_before_attaching(before, terminal)
    assert returncode == 0
    assert took < 1.5


def test_pause_after_warnings_waits_at_a_terminal_until_enter():
    # tmux's screen covers what was printed just before it, so a warning gets a moment to be read.
    returncode, shown, took, waiting_at_enter = pause_before_attaching("warn something", True, enter_after=1)
    assert (returncode, waiting_at_enter) == (0, True)
    assert "press Enter" in shown, shown
    assert took < 1.8


def test_pause_after_warnings_goes_on_by_itself_after_its_pause(tmp_path):
    returncode, shown, took, _ = pause_before_attaching("warn something", True)
    assert returncode == 0
    assert "continuing in 2 s" in shown, shown
    assert 2 <= took < 4
