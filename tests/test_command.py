"""Run staymux as a command, as its user would, and check what they see: attaching, listing, the messages.

Each test gets a host of its own: a private /run/user/$UID, a home with a tmux.conf that pins what panes run, and
tests/host first on PATH, where stubs stand in for systemd, logind and the PID namespace, and tests/host/staymux runs
staymux itself (it says why it stands in for the kernel's cgroups too). tmux is real. The stubs keep their state in
the test's host folder: calls (every call), unexpected (calls staymux has no business making, which fail the test),
linger, policy, run-fails, pid-ns and jobs, each described in the stub that reads it.
"""

import fcntl
import os
import pty
import shlex
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not shutil.which("tmux"), reason="native tmux required")

HOST = Path(__file__).resolve().parent / "host"
STAYMUX = HOST / "staymux"
SCRIPT = HOST.parent.parent / "staymux"
BASH = shutil.which("bash") or "/bin/bash"
UID = os.getuid()
ME = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
RUN = Path(f"/run/user/{UID}")  # where staymux always looks; the sandbox's /run is its own
SOCK = RUN / "staymux.sock"
HOSTNAME = "testhost"  # bash keeps the one it is given, so no message names the machine running the tests
# What panes run, pinned: a first pane waits, and a command given to new-window runs in sh, not the login shell.
TMUX_CONF = "set -g default-shell /bin/sh\nset -g default-command 'exec sleep 3600'\n"


def wait_until(condition, timeout, step=0.02):
    """Call condition until it returns something true and return that, or its last answer after timeout seconds."""
    deadline = time.monotonic() + timeout
    while True:
        found = condition()
        if found or time.monotonic() > deadline:
            return found
        time.sleep(step)


def started_by(state):
    """The processes whose environment names state as their STAYMUX_TEST_HOST.

    That is whatever one test started, from staymux to the tmux server and its panes, and nothing else.
    """
    marker = f"STAYMUX_TEST_HOST={state}".encode()
    pids = []
    for entry in Path("/proc").iterdir():
        try:
            if (
                entry.name.isdigit()
                and int(entry.name) != os.getpid()
                and marker in (entry / "environ").read_bytes().split(b"\0")
            ):
                pids.append(int(entry.name))
        except OSError:  # gone meanwhile, or not ours
            pass
    return pids


def sweep(state):
    """End the processes one test started, and any they start meanwhile, and wait until they have gone."""
    for _ in range(50):
        pids = started_by(state)
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        wait_until(lambda pids=pids: not set(pids) & set(started_by(state)), 1)
    pytest.fail(f"processes of {state} kept starting: {started_by(state)}")


def gone_pid():
    """The PID of a process that has ended."""
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


class Terminal:
    """A run of staymux on a terminal of its own, 80 columns by 24 lines, read as the user would see it."""

    def __init__(self, host, argv, env):
        self.host = host
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                os.execve(argv[0], argv, env)
            finally:
                os._exit(127)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        self.shown = bytearray()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.status = None

    def _read(self):
        while True:
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                return
            if not chunk:
                return
            self.shown += chunk

    def _ended(self):
        if self.status is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -os.WTERMSIG(status)
        return self.status is not None

    def client(self):
        """This run's tmux client as tmux lists it: its tty and session, or None while it has none."""
        # staymux execs tmux attach-session, so the client keeps this run's PID.
        listed = self.host.tmux("list-clients", "-F", "#{client_pid} #{client_tty} #{session_name}").stdout
        for line in listed.splitlines():
            pid, tty, session = line.split(" ", 2)
            if pid == str(self.pid):
                return tty, session
        return None

    def attached(self, timeout=10):
        """The session this run attached its terminal to, once it has, or None if it ended without attaching."""
        found = wait_until(lambda: self.client() or (self._ended() and "ended"), timeout)
        assert found, f"staymux neither attached nor ended within {timeout}s:\n{self.text()}"
        return None if found == "ended" else found[1]

    def session(self):
        found = self.client()
        return found and found[1]

    def finish(self, timeout=10):
        """Detach this run's client, if it has one, and return its exit status and what it showed."""
        found = self.client()
        if found:
            self.host.tmux("detach-client", "-t", found[0])
        assert wait_until(self._ended, timeout), f"staymux did not end within {timeout}s:\n{self.text()}"
        self.reader.join(timeout)
        return self.status, self.text()

    def text(self):
        return self.shown.decode(errors="replace").replace("\r\n", "\n")

    def close(self):
        if not self._ended():
            os.kill(self.pid, signal.SIGKILL)
            wait_until(self._ended, 5)
        self.reader.join(5)  # it ends once the terminal hangs up; its fd must not be reused under it
        os.close(self.fd)


class Host:
    """The host a test runs staymux on; see the module's docstring."""

    def __init__(self, tmp_path):
        self.state = tmp_path / "host"
        (self.state / "cgroup").mkdir(parents=True)
        (self.state / "linger").write_text(f"{UID}\n")
        self.pid_namespace("initial")
        self.home = tmp_path / "home"
        self.home.mkdir()
        (self.home / ".tmux.conf").write_text(TMUX_CONF)
        self.terminals = []
        self.panes = 0

    def pid_namespace(self, which):
        """Say which PID namespace staymux runs in: initial, child or unknown (see tests/host/readlink)."""
        (self.state / "pid-ns").write_text(f"{which}\n")

    def env(self, **changes):
        """The environment of a login on this host, with the given changes; a change to None unsets a variable."""
        env = {name: value for name, value in os.environ.items() if not name.startswith(("TMUX", "XDG_"))}
        env.update(
            PATH=f"{HOST}{os.pathsep}{os.environ['PATH']}",
            HOME=str(self.home),
            HOSTNAME=HOSTNAME,
            TERM="xterm-256color",
            STAYMUX_TEST_HOST=str(self.state),
        )
        for name, value in changes.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return env

    def run(self, *args, **env_changes):
        """Run staymux without a terminal and return the result."""
        return subprocess.run(
            [str(STAYMUX), *args],
            env=self.env(**env_changes),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def terminal(self, *args, command=(str(STAYMUX),), **env_changes):
        """Start staymux, or the command given, on a terminal of its own."""
        terminal = Terminal(self, [*command, *args], self.env(**env_changes))
        self.terminals.append(terminal)
        return terminal

    def tmux(self, *args):
        """Run a tmux command on staymux's socket, as the user would."""
        return subprocess.run(
            ["tmux", "-S", str(SOCK), *args], env=self.env(), capture_output=True, text=True, timeout=10, check=False
        )

    def sessions(self):
        return self.tmux("list-sessions", "-F", "#{session_name}").stdout.split()

    def server_pid(self):
        return int(self.tmux("display-message", "-p", "#{pid}").stdout)

    def started(self, name):
        """Start session name with staymux, as a user who has since detached from it."""
        terminal = self.terminal(name)
        assert terminal.attached() == name, terminal.text()
        status, shown = terminal.finish()
        assert status == 0, shown

    def in_pane(self, session, *args, ends_server=False):
        """Run staymux from a new window in session, as from a shell in one of its panes.

        Return its exit status, what it printed and what it printed as errors. With ends_server, staymux is to end
        the server and the pane with it, so its status is None if the pane has gone by then.
        """
        self.panes += 1
        out, err, status = (self.state / f"pane{self.panes}.{part}" for part in ("out", "err", "status"))
        command = f"{' '.join(shlex.quote(word) for word in [str(STAYMUX), *args])} >{shlex.quote(str(out))} 2>{shlex.quote(str(err))}"
        command += f"; echo $? >{shlex.quote(str(status))}"
        created = self.tmux("new-window", "-t", f"={session}:", command)
        assert created.returncode == 0, created.stderr

        def ended():
            if status.exists() and status.read_text().endswith("\n"):
                return True
            return ends_server and self.tmux("has-session").returncode != 0

        if not wait_until(ended, 20):
            said = "".join(path.read_text() for path in (out, err) if path.exists())
            pytest.fail(f"staymux did not end in the pane within 20s:\n{said}")
        code = status.read_text() if status.exists() else ""
        return int(code) if code.endswith("\n") else None, out.read_text(), err.read_text()

    def calls(self, command):
        """The calls staymux made to one of the stubbed commands, each as its arguments."""
        log = self.state / "calls"
        lines = log.read_text().splitlines() if log.exists() else []
        return [line.partition(" ")[2] for line in lines if line.partition(" ")[0] == command]

    def close(self):
        for terminal in self.terminals:
            terminal.close()
        self.tmux("kill-server")  # even one whose environment no longer names this test (see started_by)
        sweep(self.state)
        for made in ("/run/user", "/run/systemd"):  # the fixture made both; see host()
            shutil.rmtree(made, ignore_errors=True)
        unexpected = self.state / "unexpected"
        assert not unexpected.exists(), "staymux made calls no stub expects:\n" + unexpected.read_text()


@pytest.fixture
def host(tmp_path):
    # The sandbox's /run starts empty and holds only what these tests make, so anything else is a real one, which a
    # test must never empty.
    found = sorted(set(os.listdir("/run")) - {"user", "systemd"})
    if found:
        pytest.fail(f"/run holds {', '.join(found)}, so it is not the sandbox's own; run the tests with ./run-tests")
    shutil.rmtree(RUN, ignore_errors=True)
    RUN.mkdir(mode=0o700, parents=True)
    Path("/run/systemd/system").mkdir(parents=True, exist_ok=True)
    host = Host(tmp_path)
    try:
        yield host
    finally:
        host.close()


# --- a shell whose $TMUX names staymux's socket ---------------------------------------------------------------------


def test_a_shell_left_with_the_tmux_of_a_server_that_has_gone_attaches_to_a_new_one(host):
    # After a crash a shell outside staymux can still carry $TMUX naming its socket. In the initial PID namespace a
    # server that isn't visible has gone, so staymux acts as it would outside tmux, rather than list sessions to
    # switch to from a pane that isn't there.
    terminal = host.terminal(TMUX=f"{SOCK},{gone_pid()},0", TMUX_PANE="%0")
    assert terminal.attached() == HOSTNAME, terminal.text()
    status, shown = terminal.finish()
    assert status == 0, shown
    assert f"[detached (from session {HOSTNAME})]" in shown
    assert "Switch with" not in shown


@pytest.mark.parametrize("pid_namespace", ["child", "unknown"])
def test_a_shell_in_another_pid_namespace_trusts_the_tmux_of_a_server_it_cannot_see(host, pid_namespace):
    # There, as in a sandbox started from a pane, the server may run outside, out of sight, so the shell still counts
    # as inside staymux: plain staymux lists the sessions to switch to, none here, rather than start a second server.
    host.pid_namespace(pid_namespace)
    result = host.run(TMUX=f"{SOCK},{gone_pid()},0", TMUX_PANE="%0")
    assert (result.returncode, result.stderr) == (0, "")
    assert result.stdout == "  NAME  WINDOWS  CLIENTS  CREATED\n\nSwitch with: staymux NAME\n"
    assert host.sessions() == []


def test_staymux_in_a_pane_lists_the_sessions_marking_its_own_and_switches_to_another(host):
    terminal = host.terminal("alpha")
    assert terminal.attached() == "alpha", terminal.text()
    status, listed, errors = host.in_pane("alpha")
    assert (status, errors) == (0, "")
    lines = listed.splitlines()
    assert lines[0] == "  NAME   WINDOWS  CLIENTS  CREATED"
    assert [line[:8] for line in lines[1:]] == ["* alpha ", "", "Switch w"]
    assert lines[-1] == "Switch with: staymux NAME"

    assert host.in_pane("alpha", "beta") == (0, "", "")
    assert (terminal.session(), host.sessions()) == ("beta", ["alpha", "beta"])
    status, shown = terminal.finish()
    assert status == 0, shown
    assert "[detached (from session beta)]" in shown


def test_a_process_that_only_inherited_the_tmux_of_a_running_server_attaches_as_from_outside(host):
    # Started from a pane and since moved away, such as an editor's terminal: switch-client would act on the pane's
    # client instead, so it counts as outside staymux, and plain staymux attaches to the one session.
    host.started("alpha")
    terminal = host.terminal(TMUX=f"{SOCK},{host.server_pid()},0", TMUX_PANE="%0")
    assert terminal.attached() == "alpha", terminal.text()
    status, shown = terminal.finish()
    assert status == 0, shown
    assert "Switch with" not in shown


def test_staymux_refuses_to_run_inside_another_tmux(host):
    result = host.run(TMUX="/tmp/tmux-1000/default,4242,0", TMUX_PANE="%3")
    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == (
        "staymux: this shell runs inside another tmux server (/tmp/tmux-1000/default); staymux does not nest tmux.\n"
        "  Detach from it first (prefix key, then d), or use a terminal outside tmux, then run staymux again.\n"
    )
    assert host.sessions() == []


# --- staymux and staymux NAME --------------------------------------------------------------------------------------


def test_staymux_starts_its_server_in_a_scope_of_staymux_slice(host):
    # The scope is what outlives logout. SendSIGHUP: stopping it also ends interactive shells, which ignore SIGTERM.
    terminal = host.terminal()
    assert terminal.attached() == HOSTNAME, terminal.text()
    [run] = host.calls("systemd-run")
    words = run.split()
    unit = next(word for word in words if word.startswith("--unit="))
    assert unit.startswith("--unit=staymux-") and unit.endswith(".scope")
    assert words[: words.index(unit)] == ["--user", "--scope", "--quiet"]
    assert run.partition(f"{unit} ")[2] == (
        "--slice=staymux.slice --description=staymux tmux server --property=SendSIGHUP=yes "
        f"tmux -S {SOCK} new-session -d -s {HOSTNAME}"
    )
    assert terminal.finish()[0] == 0


def test_staymux_name_creates_that_session_beside_the_others_and_attaches_to_it(host):
    host.started("alpha")
    server = host.server_pid()
    terminal = host.terminal("beta")
    assert terminal.attached() == "beta", terminal.text()
    assert (host.sessions(), host.server_pid()) == (["alpha", "beta"], server)
    assert len(host.calls("systemd-run")) == 1  # the one server
    assert terminal.finish()[0] == 0


def test_staymux_name_attaches_to_a_session_that_is_already_there(host):
    host.started("alpha")
    host.started("beta")
    terminal = host.terminal("alpha")
    assert terminal.attached() == "alpha", terminal.text()
    assert host.sessions() == ["alpha", "beta"]
    assert terminal.finish()[0] == 0


def test_staymux_lists_several_sessions_rather_than_pick_one(host):
    host.started("alpha")
    host.started("beta")
    result = host.run()
    assert (result.returncode, result.stderr) == (0, "")
    lines = result.stdout.splitlines()
    assert lines[:2] == ["Several staymux sessions are running:", ""]
    assert lines[2] == "  NAME   WINDOWS  CLIENTS  CREATED"
    assert [line.split()[:3] for line in lines[3:5]] == [["alpha", "1", "0"], ["beta", "1", "0"]]
    assert lines[5:] == ["", "Attach to one with: staymux NAME"]


@pytest.mark.parametrize("name", ["-x", "a.b", "all", "x" * 65], ids=["leading-dash", "dot", "reserved", "too-long"])
def test_staymux_name_refuses_a_name_it_cannot_create(host, name):
    result = host.run("--", name)
    assert (result.returncode, result.stdout) == (2, "")
    assert f"staymux: invalid session name '{name}'" in result.stderr
    assert host.calls("systemd-run") == []


@pytest.mark.parametrize("variable", ["XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"])
@pytest.mark.parametrize(
    ("folder", "kept"),
    [("own", True), ("/usr", False), ("missing", False)],
    ids=["own-folder", "someone-elses-folder", "missing-folder"],
)
def test_the_sessions_get_a_login_s_own_environment_whatever_staymux_inherited(host, tmp_path, variable, folder, kept):
    # After `su -m` or `sudo -E`, the caller's XDG_CONFIG_HOME would give the server their tmux.conf, and the others
    # would give plugins their saved files, such as tmux-resurrect's sessions under XDG_DATA_HOME. Their runtime
    # directory and bus would lead systemctl to their manager, and USER and LOGNAME would name them.
    folder = {"own": str(tmp_path), "missing": str(tmp_path / "missing")}.get(folder, folder)
    inherited = {variable: folder, "USER": "someone", "LOGNAME": "someone"}
    inherited.update(XDG_RUNTIME_DIR="/run/user/0", DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/0/bus")
    terminal = host.terminal("alpha", **inherited)
    assert terminal.attached() == "alpha", terminal.text()
    pane = host.tmux("display-message", "-p", "-t", "=alpha:", "#{pane_pid}").stdout.strip()
    env = dict(entry.split("=", 1) for entry in Path(f"/proc/{pane}/environ").read_text().split("\0") if entry)
    assert env.get(variable) == (folder if kept else None)
    assert (env["USER"], env["LOGNAME"], env["HOME"]) == (ME, ME, str(host.home))
    assert env["XDG_RUNTIME_DIR"] == str(RUN)
    assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={RUN}/bus"
    assert terminal.finish()[0] == 0


@pytest.mark.parametrize(
    "inherited", [{"SHELLOPTS": "noclobber"}, {"TMPDIR": "/proc"}], ids=["noclobber", "unwritable-tmpdir"]
)
def test_staymux_starts_and_attaches_whatever_the_caller_s_shell_settings(host, inherited):
    # Both reach staymux through `su -m` or `sudo -E`.
    terminal = host.terminal("alpha", **inherited)
    assert terminal.attached() == "alpha", terminal.text()
    assert terminal.finish()[0] == 0


def test_staymux_reports_a_scope_that_systemd_refuses(host):
    (host.state / "run-fails").touch()
    terminal = host.terminal("alpha")
    assert terminal.attached() is None
    status, shown = terminal.finish()
    assert status == 1
    assert "Failed to start transient scope unit: Access denied\n" in shown, shown
    assert "staymux: could not start the tmux server (see the error above).\n" in shown, shown
    assert host.sessions() == []


def test_staymux_turns_lingering_on_before_it_starts_a_server(host):
    (host.state / "linger").unlink()
    terminal = host.terminal("alpha")
    assert terminal.attached() == "alpha", terminal.text()
    status, shown = terminal.finish()
    assert status == 0, shown
    assert (
        f"staymux: enabling lingering for {ME} so that sessions survive logout (loginctl enable-linger {UID}).\n"
        in shown
    )
    assert (host.state / "linger").read_text().strip() == str(UID)


def test_staymux_does_not_wait_for_a_password_nobody_can_give_for_lingering(host):
    # loginctl would ask through any polkit agent, its own or one already running, even with --no-ask-password,
    # and hold the lock until someone typed a password into this terminal.
    (host.state / "linger").unlink()
    (host.state / "policy").write_text("admin\n")
    terminal = host.terminal("alpha")
    assert terminal.attached(timeout=5) is None, "staymux attached"
    status, shown = terminal.finish()
    assert status == 1
    assert "Interactive authentication required" in shown, shown
    assert "staymux: lingering is off for" in shown and "so a new server would be killed at logout" in shown, shown
    assert f"Or ask an administrator to run: sudo loginctl enable-linger {UID}\n" in shown, shown
    assert host.sessions() == []


def in_a_cgroup_namespace():
    """True if this process sees itself at the root of every cgroup tree, as in a cgroup namespace of its own."""
    return all(line.split(":", 2)[2] == "/" for line in Path("/proc/self/cgroup").read_text().splitlines())


@pytest.mark.skipif(not in_a_cgroup_namespace(), reason="needs the cgroup namespace the sandbox makes where it can")
def test_staymux_as_it_is_refuses_a_server_whose_cgroup_it_cannot_see(host):
    # The plain file, without tests/host/staymux standing in for the kernel: in a cgroup namespace such as this
    # sandbox's, no process shows which unit it runs in, so staymux cannot check that its server outlives logout.
    terminal = host.terminal("alpha", command=(BASH, str(SCRIPT)))
    assert terminal.attached() is None
    status, shown = terminal.finish()
    assert status == 1
    assert "so cannot check that it runs in staymux.slice and outlives logout.\n" in shown, shown
    assert "  If this runs in a container or sandbox, run staymux outside it.\n" in shown, shown


# --- staymux -l ------------------------------------------------------------------------------------------------------


def test_staymux_l_says_when_no_session_runs(host):
    result = host.run("-l")
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "No staymux sessions are running. Start one with: staymux [NAME]\n",
        "",
    )


def test_staymux_l_lists_the_sessions_with_their_attached_clients(host):
    host.started("alpha")
    terminal = host.terminal("beta")
    assert terminal.attached() == "beta", terminal.text()
    result = host.run("-l")
    assert (result.returncode, result.stderr) == (0, "")
    lines = result.stdout.splitlines()
    assert lines[0] == "  NAME   WINDOWS  CLIENTS  CREATED"
    assert [line[:2] + " ".join(line.split()[:3]) for line in lines[1:]] == ["  alpha 1 0", "  beta 1 1"]
    assert terminal.finish()[0] == 0


def test_staymux_l_in_a_pane_marks_its_session(host):
    host.started("alpha")
    terminal = host.terminal("beta")
    assert terminal.attached() == "beta", terminal.text()
    status, listed, errors = host.in_pane("beta", "-l")
    assert (status, errors) == (0, "")
    assert [line[:7] for line in listed.splitlines()[1:]] == ["  alpha", "* beta "]
    assert terminal.finish()[0] == 0


# --- staymux -k ------------------------------------------------------------------------------------------------------


def test_staymux_k_kills_one_session_and_leaves_the_others(host):
    host.started("alpha")
    host.started("beta")
    result = host.run("-k", "alpha")
    assert (result.returncode, result.stdout, result.stderr) == (0, "Killed session 'alpha'.\n", "")
    assert host.sessions() == ["beta"]


def test_staymux_k_says_when_the_last_session_ends_the_server(host):
    host.started("alpha")
    result = host.run("-k", "alpha")
    assert (result.returncode, result.stderr) == (0, "")
    assert result.stdout == "Killed session 'alpha'.\nThat was the last session, so the staymux server has exited.\n"
    assert host.sessions() == []


def test_staymux_k_names_a_session_that_is_not_there(host):
    host.started("alpha")
    result = host.run("-k", "alph")  # names are matched exactly, not by prefix as tmux would
    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == "staymux: no session named 'alph'.\n  See the running sessions with: staymux -l\n"
    assert host.sessions() == ["alpha"]


def test_staymux_k_all_kills_every_session_and_stops_the_slice(host):
    host.started("alpha")
    host.started("beta")
    result = host.run("-k", "all")
    assert (result.returncode, result.stdout, result.stderr) == (0, "Killed 2 staymux session(s) and the server.\n", "")
    assert host.sessions() == []
    assert "--user stop staymux.slice" in host.calls("systemctl")


def test_staymux_k_all_cleans_up_the_slice_when_no_server_runs(host):
    result = host.run("-k", "all")
    assert (result.returncode, result.stderr) == (0, "")
    assert result.stdout == "No staymux server was running; cleaned up staymux.slice.\n"
    assert "--user stop staymux.slice" in host.calls("systemctl")


def test_staymux_k_all_in_a_pane_queues_the_stop_and_ends_the_server_with_this_pane(host):
    # This pane is among what stops, so systemd is left to finish the stop after staymux has gone.
    terminal = host.terminal("alpha")
    assert terminal.attached() == "alpha", terminal.text()
    _, said, errors = host.in_pane("alpha", "-k", "all", ends_server=True)
    assert (said, errors) == ("Stopping all staymux sessions, including this one.\n", "")
    assert host.sessions() == []
    assert "--user stop --no-block staymux.slice" in host.calls("systemctl")
    assert "--user stop staymux.slice" not in host.calls("systemctl")
