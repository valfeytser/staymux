"""Run staymux as a command, as its user would, and check what they see: attaching, listing, the messages.

Each test gets a host of its own: a private /run/user/$UID, a home with a tmux.conf that pins what panes run, and
tests/host first on PATH, where stubs stand in for systemd, logind and the PID namespace, and tests/host/staymux runs
staymux itself (it says why it stands in for the kernel's cgroups too). tmux is real. The stubs keep their state in
the test's host folder: calls (every call), unexpected (calls staymux has no business making, which fail the test),
linger, policy, run-fails, pid-ns and cgroups, each described in the stub that reads it.
"""

import contextlib
import fcntl
import os
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
_ID = subprocess.run(["id", "-un"], capture_output=True, text=True, check=False)
ME = _ID.stdout.strip() if _ID.returncode == 0 else ""  # without a passwd entry, id prints the UID and fails
RUN = Path(f"/run/user/{UID}")  # where staymux always looks; the sandbox's /run is its own
SOCK = RUN / "staymux.sock"
XDG_FOLDERS = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")
# Longer than staymux waits for the lock or a server's start, so that its own message shows when either stalls.
STAYMUX_WAITS = 35
POLL = 0.05  # how often to ask tmux, each time a client of its own
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
        self.fd, side = os.openpty()
        self.tty = os.ttyname(side)
        fcntl.ioctl(side, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        self.pid = os.fork()
        if self.pid == 0:  # as pty.fork, which doesn't say which terminal it made
            try:
                os.setsid()
                os.close(self.fd)
                for fd in (0, 1, 2):
                    os.dup2(side, fd)
                os.close(side)
                os.close(os.open(self.tty, os.O_RDWR))  # the first terminal a session leader opens becomes its own
                os.execve(argv[0], argv, env)
            finally:
                os._exit(127)
        os.close(side)
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

    def session(self):
        """The session a tmux client shows on this terminal, or None while none does."""
        listed = self.host.tmux("list-clients", "-F", "#{client_tty} #{session_name}").stdout
        for line in listed.splitlines():
            tty, session = line.split(" ", 1)
            if tty == self.tty:
                return session
        return None

    def attached(self, timeout=STAYMUX_WAITS):
        """The session this run attached its terminal to, once it has, or None if it ended without attaching."""
        ended = object()
        found = wait_until(lambda: self.session() or (self._ended() and ended), timeout, step=POLL)
        assert found, f"staymux neither attached nor ended within {timeout}s:\n{self.text()}"
        return None if found is ended else found

    def finish(self, timeout=10):
        """Detach this run's client, if it has one, and return its exit status and what it showed."""
        if self.session():
            self.host.tmux("detach-client", "-t", self.tty)
        assert wait_until(self._ended, timeout), f"staymux did not end within {timeout}s:\n{self.text()}"
        self.reader.join(timeout)
        assert not self.reader.is_alive(), f"something still holds the terminal staymux ran on:\n{self.text()}"
        return self.status, self.text()

    def text(self):
        return self.shown.decode(errors="replace").replace("\r\n", "\n")

    def end(self):
        if not self._ended():
            os.kill(self.pid, signal.SIGKILL)
            wait_until(self._ended, 5)

    def close(self):
        """Close the terminal, once nothing holds its other side (see Host.close)."""
        self.reader.join(5)  # it ends once the terminal hangs up
        if not self.reader.is_alive():  # else leave the fd open, so it can't be reused under the reader
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

    def cgroups(self, layout):
        """Say how /proc/PID/cgroup reads, "{cgroup}" standing for the process's cgroup (see tests/host/staymux)."""
        (self.state / "cgroups").write_text(layout)

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

    def run(self, *args, command=(str(STAYMUX),), **env_changes):
        """Run staymux, or the command given, without a terminal and return the result."""
        return subprocess.run(
            [*command, *args],
            env=self.env(**env_changes),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=40,  # longer than staymux waits for the lock, so its own message shows
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
        staymux = " ".join(shlex.quote(word) for word in [str(STAYMUX), *args])
        command = f"{staymux} >{shlex.quote(str(out))} 2>{shlex.quote(str(err))}; echo $? >{shlex.quote(str(status))}"
        created = self.tmux("new-window", "-t", f"={session}:", command)
        assert created.returncode == 0, created.stderr

        def ended():
            if status.exists() and status.read_text().endswith("\n"):
                return True
            return ends_server and self.tmux("has-session").returncode != 0

        def read(path):
            return path.read_text() if path.exists() else ""  # a pane can go before its shell opens them

        if not wait_until(ended, STAYMUX_WAITS, step=POLL):
            pytest.fail(f"staymux did not end in the pane within {STAYMUX_WAITS}s:\n{read(out)}{read(err)}")
        code = read(status)
        return int(code) if code.endswith("\n") else None, read(out), read(err)

    def calls(self, command=None):
        """The calls staymux made to the stubbed commands, in order, each as the list of its arguments.

        Given a command, its calls only, without its name.
        """
        log = self.state / "calls"
        calls = [shlex.split(line) for line in log.read_text().splitlines()] if log.exists() else []
        return calls if command is None else [call[1:] for call in calls if call[0] == command]

    def close(self):
        """End what the test started and remove what host() made, even if a step fails."""
        try:
            for terminal in self.terminals:
                terminal.end()
            try:
                self.tmux("kill-server")  # even one whose title has overwritten its environment, on some builds
            except subprocess.TimeoutExpired:
                pass  # a stopped server, say, which the sweep ends
            sweep(self.state)
        finally:
            for terminal in self.terminals:
                terminal.close()
            for made in (RUN, Path("/run/systemd/system")):
                shutil.rmtree(made, ignore_errors=True)
            # host() made these too; remove them if nothing else is in them.
            for parent in ("/run/user", "/run/systemd"):
                with contextlib.suppress(OSError):
                    os.rmdir(parent)
        unexpected = self.state / "unexpected"
        assert not unexpected.exists(), "staymux made calls no stub expects:\n" + unexpected.read_text()


@pytest.fixture
def host(tmp_path):
    # A real /run belongs to root; the sandbox mounts one of its own, owned by whoever runs the tests and empty but
    # for what these tests make, which each removes again. So no test can empty a real runtime directory.
    if os.stat("/run").st_uid != UID:
        pytest.fail("/run belongs to someone else, so it is not the sandbox's own; run the tests with ./run-tests")
    if os.listdir("/run"):
        found = ", ".join(sorted(os.listdir("/run")))
        pytest.fail(f"/run holds {found}: a test before this one left it, or this is not the sandbox's own /run")
    host = Host(tmp_path)
    try:
        RUN.mkdir(mode=0o700, parents=True)
        Path("/run/systemd/system").mkdir(parents=True)
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
    options, command = run[: run.index("tmux")], run[run.index("tmux") :]
    assert {"--user", "--scope", "--slice=staymux.slice", "--property=SendSIGHUP=yes"} <= set(options)
    [unit] = [option for option in options if option.startswith("--unit=")]
    assert unit.startswith("--unit=staymux-") and unit.endswith(".scope")
    assert command[:4] == ["tmux", "-S", str(SOCK), "new-session"]
    assert "-d" in command[4:] and command[command.index("-s") + 1] == HOSTNAME
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


@pytest.mark.parametrize(
    "kinds",
    [
        ("own", "else", "missing", "else"),
        ("missing", "own", "else", "missing"),
        ("else", "missing", "own", "else"),
        ("missing", "else", "else", "own"),
    ],
    ids=lambda kinds: "-".join(kinds),
)
@pytest.mark.skipif(not ME, reason="needs a passwd entry for the user running the tests, as staymux reads it")
def test_the_sessions_get_a_login_s_own_environment_whatever_staymux_inherited(host, tmp_path, kinds):
    # After `su -m` or `sudo -E`, the caller's XDG_CONFIG_HOME would give the server their tmux.conf, and the others
    # would give plugins their saved files, such as tmux-resurrect's sessions under XDG_DATA_HOME. Their runtime
    # directory and bus would lead systemctl to their manager, and USER and LOGNAME would name them. Each run gives
    # one variable a folder of our own and the others someone else's or missing ones, so that a variable judged by
    # another's folder fails.
    inherited, expected = {}, {}
    for variable, kind in zip(XDG_FOLDERS, kinds):
        folder = {"own": tmp_path / variable, "else": Path("/usr"), "missing": tmp_path / "missing" / variable}[kind]
        if kind == "own":
            folder.mkdir()
        inherited[variable] = str(folder)
        expected[variable] = str(folder) if kind == "own" else None
    inherited.update(USER="someone", LOGNAME="someone")
    inherited.update(XDG_RUNTIME_DIR="/run/user/0", DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/0/bus")
    terminal = host.terminal("alpha", **inherited)
    assert terminal.attached() == "alpha", terminal.text()
    pane = host.tmux("display-message", "-p", "-t", "=alpha:", "#{pane_pid}").stdout.strip()
    env = dict(entry.split("=", 1) for entry in Path(f"/proc/{pane}/environ").read_text().split("\0") if entry)
    assert {variable: env.get(variable) for variable in XDG_FOLDERS} == expected
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


@pytest.mark.skipif(not ME, reason="needs a passwd entry for the user running the tests, as staymux reads it")
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
    commands = [call[0] for call in host.calls()]
    assert commands.index("busctl") < commands.index("systemd-run")


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
    # The server runs already, so the file never starts one, nor reads the host's own cgroup tree for leftovers.
    host.started("alpha")
    terminal = host.terminal("beta", command=(BASH, str(SCRIPT)))
    assert terminal.attached() is None
    status, shown = terminal.finish()
    assert status == 1
    assert "so cannot check that it runs in staymux.slice and outlives logout.\n" in shown, shown
    assert "  If this runs in a container or sandbox, run staymux outside it.\n" in shown, shown
    assert host.sessions() == ["alpha"]


@pytest.mark.parametrize(
    ("args", "said"),
    [
        ([], "Several staymux sessions are running:\n"),
        (["-k", "alpha"], "Killed session 'alpha'.\n"),
        (["-l"], "  NAME "),
    ],
    ids=["staymux", "staymux-k-name", "staymux-l"],
)
def test_staymux_as_installed_hands_its_arguments_on(host, args, said):
    # The other tests run main through tests/host/staymux. This runs the file itself, as installed, so that its last
    # line, which hands main the arguments, is tested too; none of these needs to see its server's cgroup.
    host.started("alpha")
    host.started("beta")
    result = host.run(*args, command=(str(SCRIPT),))
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(said), result.stdout


# --- which cgroup the server runs in ---------------------------------------------------------------------------------

# How /proc/PID/cgroup reads for a process in {cgroup} on each kind of host, as RHEL 8.10 and Ubuntu 24.04 show it.
# cgroup v2 has one line. RHEL 8 keeps cgroup v1, where systemd tracks processes in its name=systemd hierarchy;
# a scope of the user's own manager moves a process in that one alone, so the controllers' lines still name the login
# it came from. Once anything has mounted cgroup2 there, every process also shows at the unified hierarchy's root.
# systemd's hybrid mode tracks processes in both.
LOGIN = f"/user.slice/user-{UID}.slice/session-1.scope"
CGROUP_V1 = (
    f"12:pids:{LOGIN}\n11:net_cls,net_prio:/\n10:blkio:/system.slice/sshd.service\n"
    f"9:devices:/system.slice/sshd.service\n8:cpuset:/\n7:rdma:/\n6:freezer:/\n5:memory:{LOGIN}\n4:hugetlb:/\n"
    "3:perf_event:/\n2:cpu,cpuacct:/\n1:name=systemd:{cgroup}\n"
)
CGROUP_LAYOUTS = {
    "v2": "0::{cgroup}\n",
    "v1": CGROUP_V1,
    "v1-with-cgroup2-mounted": CGROUP_V1 + "0::/\n",
    "hybrid": CGROUP_V1 + "0::{cgroup}\n",
}


@pytest.mark.parametrize("layout", CGROUP_LAYOUTS.values(), ids=CGROUP_LAYOUTS.keys())
def test_staymux_finds_its_server_in_staymux_slice_on_each_kind_of_host(host, layout):
    # staymux refuses to attach to a new server it cannot find in the slice, and staymux -l warns of one.
    host.cgroups(layout)
    host.started("alpha")
    result = host.run("-l")
    assert (result.returncode, result.stderr) == (0, ""), result.stderr
    assert result.stdout.startswith("  NAME "), result.stdout


@pytest.mark.parametrize("layout", CGROUP_LAYOUTS.values(), ids=CGROUP_LAYOUTS.keys())
def test_staymux_l_warns_of_a_server_outside_staymux_slice_on_each_kind_of_host(host, layout):
    # One started from the login with plain tmux on staymux's socket, so it runs in the login's scope.
    host.cgroups(layout)
    started = host.tmux("new-session", "-d", "-s", "alpha")
    assert started.returncode == 0, started.stderr
    result = host.run("-l")
    assert result.returncode == 0
    assert result.stderr == (
        f"staymux: the server (PID {host.server_pid()}) runs in {LOGIN}, outside staymux.slice, so it may be killed"
        " when you log out.\n  Save your work, then run: staymux -k all; staymux\n"
    )


@pytest.mark.parametrize(
    "layout",
    ["0::/\n", "0::/..\n", "0::/../..{cgroup}\n", "1:name=systemd:/../..{cgroup}\n"],
    ids=["at-its-root", "above-its-root", "elsewhere", "elsewhere-on-cgroup-v1"],
)
def test_staymux_l_cannot_tell_where_its_server_runs_from_another_cgroup_namespace(host, layout):
    # Run in a container, staymux sees its server by a path from the container's cgroup: /, /.. or /../.., which may
    # still end in staymux.slice but says nothing of where the server runs.
    host.started("alpha")
    host.cgroups(layout)
    result = host.run("-l")
    assert result.returncode == 0
    assert result.stderr == (
        f"staymux: cannot tell which cgroup the server (PID {host.server_pid()}) runs in, so cannot check that it"
        " runs in staymux.slice.\n  If this runs in a container or sandbox, run staymux -l outside it.\n"
    )


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
    assert ["--user", "stop", "staymux.slice"] in host.calls("systemctl")


def test_staymux_k_all_cleans_up_the_slice_when_no_server_runs(host):
    result = host.run("-k", "all")
    assert (result.returncode, result.stderr) == (0, "")
    assert result.stdout == "No staymux server was running; cleaned up staymux.slice.\n"
    assert ["--user", "stop", "staymux.slice"] in host.calls("systemctl")


def test_staymux_k_all_in_a_pane_queues_the_stop_and_ends_the_server_with_this_pane(host):
    # This pane is among what stops, so systemd is left to finish the stop after staymux has gone.
    terminal = host.terminal("alpha")
    assert terminal.attached() == "alpha", terminal.text()
    _, said, errors = host.in_pane("alpha", "-k", "all", ends_server=True)
    assert (said, errors) == ("Stopping all staymux sessions, including this one.\n", "")
    assert host.sessions() == []
    assert ["--user", "stop", "--no-block", "staymux.slice"] in host.calls("systemctl")
    assert ["--user", "stop", "staymux.slice"] not in host.calls("systemctl")
