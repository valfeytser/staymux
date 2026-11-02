"""Exercise lost-socket recovery with real processes inside the test sandbox."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not all(shutil.which(command) for command in ("tmux", "python3")),
    reason="native tmux and python3 required",
)

SCRIPT = Path(__file__).resolve().parents[1] / "staymux"


def tmux_version():
    if not shutil.which("tmux"):
        return (0, 0)
    output = subprocess.run(["tmux", "-V"], capture_output=True, text=True, timeout=5, check=False).stdout
    found = re.search(r"(\d+)\.(\d+)", output)
    return (int(found[1]), int(found[2])) if found else (0, 0)


def start_listener(address):
    """Bash that starts a Python process, $holder, listening on address, a Python expression of $SOCK (sys.argv[1])."""
    return (
        r"""python3 - "$SOCK" >"$SOCK.ready" <<'PYTHON' &
import signal
import socket
import sys

listener = socket.socket(socket.AF_UNIX)
listener.bind("""
        + address
        + r""")
listener.listen()
print("ready", flush=True)
signal.pause()
PYTHON
holder=$!
cleanup_pids+=("$holder")
for ((i = 0; i < 100; i++)); do
    [[ ! -s $SOCK.ready ]] || break
    kill -0 "$holder" 2>/dev/null || exit 1
    sleep 0.01
done
[[ -s $SOCK.ready ]] || exit 1
"""
    )


START_NON_TMUX_LISTENER = start_listener("sys.argv[1]")
# An abstract name holding a newline, which /proc/net/unix prints as it is, so that the rest reads as a line of its
# own: a listener bound to $SOCK with an inode nobody holds. Any local user can bind one.
START_FORGED_LISTING = start_listener(
    r'"\0x\n0000000000000000: 00000002 00000000 00010000 0001 01 99999 " + sys.argv[1]'
)


def nested_namespaces():
    """True if this sandbox can start a PID namespace of its own, with its own /proc, inside it."""
    if not shutil.which("unshare"):
        return False
    probe = ["unshare", "-Urp", "--kill-child", "--mount-proc", "true"]  # --kill-child forks, and ends the fork with it
    try:
        return subprocess.run(probe, capture_output=True, timeout=5, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


TEST_SETUP = r"""unset TMUX TMUX_PANE
cleanup_pids=()
cleanup() {
    local pid
    # A stopped process acts on SIGTERM only once it is continued.
    for pid in "${cleanup_pids[@]}"; do kill "$pid" 2>/dev/null; kill -CONT "$pid" 2>/dev/null; done
    wait
}
trap cleanup EXIT
start_tmux_server() {
    local socket=${1:-$SOCK}
    tmux -f /dev/null -S "$socket" new-session -d -s recovery 'exec sleep 300' || return 1
    read -r server pane < <(tmux -S "$socket" display-message -p '#{pid} #{pane_pid}')
    [[ -n $server && -n $pane ]] || return 1
    cleanup_pids+=("$server" "$pane")
}
wait_for_socket() {
    for ((i = 0; i < 1000; i++)); do
        [[ ! -S $SOCK ]] || return 0
        sleep 0.01
    done
    [[ -S $SOCK ]]
}
"""


def run_with_staymux(commands):
    # A Unix socket path holds at most 108 bytes, which a deep TMPDIR can exceed; the sandbox's /tmp is private.
    # REVIVE_TRIED starts as load_sessions leaves it: an indexed array, keyed by PID.
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; SOCK=$(mktemp -d /tmp/staymux.XXXXXX)/staymux.sock || exit 1; LOCKED=1; REVIVE_TRIED=();\n'
            + TEST_SETUP
            + commands,
            "staymux-test",
            str(SCRIPT),
        ],
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_revive_server_restores_a_real_tmux_socket():
    run_with_staymux(
        r"""start_tmux_server || exit 1
rm -- "$SOCK" || exit 1
revive_server || exit 1
[[ $REVIVED == "$server" ]] || exit 1
wait_for_socket || exit 1
tmux -S "$SOCK" has-session -t recovery || exit 1
exit 0
""",
    )


def test_revive_server_leaves_a_non_tmux_listener_running():
    run_with_staymux(
        START_NON_TMUX_LISTENER
        + r"""revive_server
status=$?
if kill -0 "$holder" 2>/dev/null; then alive=yes; else alive=no; fi
printf 'REVIVED=%s alive=%s status=%s\n' "$REVIVED" "$alive" "$status"
[[ -z $REVIVED && $alive == yes && $status == 1 ]] || exit 1
exit 0
""",
    )


def test_holds_socket_rejects_a_non_tmux_listener():
    run_with_staymux(
        START_NON_TMUX_LISTENER
        + r"""holds_socket "$holder"
status=$?
printf 'holds_socket status=%s\n' "$status"
[[ $status == 1 ]] || exit 1
exit 0
""",
    )


@pytest.mark.skipif(not nested_namespaces(), reason="needs a PID namespace inside the sandbox")
@pytest.mark.parametrize(
    "listing",
    ["", START_FORGED_LISTING, START_NON_TMUX_LISTENER],
    ids=["no-listener", "forged-listing", "real-listener"],
)
@pytest.mark.parametrize(
    ("namespace", "status"),
    [("initial", 1), ("child", 2), ("unknown", 2)],
    ids=["initial-namespace", "child-namespace", "namespace-unknown"],
)
def test_holds_socket_of_a_process_not_visible_here_goes_by_the_pid_namespace_alone(listing, namespace, status):
    # In the initial PID namespace every process of ours is visible, so one that isn't has gone; in any other
    # (a sandbox started from a pane, say) it may be the server, outside. Nothing listed for $SOCK changes that:
    # another user can forge a line of /proc/net/unix, or bind a real socket to the same path in a mount namespace
    # of their own. The check runs in a PID namespace of its own, so the listener's holder is not visible to it;
    # this sandbox's own namespace stands in for the initial one where the test says so.
    run_with_staymux(
        listing
        + r"""unshare -Urp --kill-child --mount-proc bash -c '
source "$1"; SOCK=$2
[[ $3 == child ]] || INIT_PID_NS=$(readlink /proc/self/ns/pid)
[[ $3 != unknown ]] || readlink() { return 1; }
holds_socket 999999999
echo "status=$?"' _ "$1" "$SOCK" """
        + f"""{namespace} >"$SOCK.out" 2>&1
read -r line <"$SOCK.out"
[[ $line == status={status} ]] || {{ cat "$SOCK.out"; exit 1; }}
exit 0
""",
    )


@pytest.mark.parametrize(
    "command",
    [["bash", str(SCRIPT), "-V"], ["bash", "-s", "--", "-V"], ["bash", SCRIPT.name, "-V"]],
    ids=["file", "stdin", "path-lookup"],
)
def test_version_as_a_command(command):
    # Given a name that isn't in the current folder, bash looks it up in PATH, as for `bash -x staymux`.
    version = subprocess.run(
        ["bash", "-c", 'source "$1"; printf %s "$VERSION"', "staymux-test", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    ).stdout
    result = subprocess.run(
        command,
        input=SCRIPT.read_text() if "-s" in command else None,
        cwd="/",
        env={**os.environ, "PATH": f"{SCRIPT.parent}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"staymux {version}\n"


def test_is_our_server_rejects_a_real_tmux_client():
    run_with_staymux(
        r"""start_tmux_server || exit 1
tmux -S "$SOCK" wait-for some-channel &
client=$!
cleanup_pids+=("$client")
for ((i = 0; i < 1000; i++)); do
    IFS= read -r comm 2>/dev/null <"/proc/$client/comm"
    [[ $comm != tmux* ]] || break
    kill -0 "$client" 2>/dev/null || exit 1
    sleep 0.01
done
[[ $comm == tmux* ]] || exit 1
is_our_server "$server" || exit 1
if is_our_server "$client"; then
    printf 'accepted tmux client %s (%s) as our server\n' "$client" "$comm"
    exit 1
fi
exit 0
""",
    )


def test_is_our_server_rejects_a_tmux_server_for_another_socket():
    run_with_staymux(
        r"""start_tmux_server "$SOCK.other" || exit 1
if is_our_server "$server"; then
    printf 'accepted server %s for another socket\n' "$server"
    exit 1
fi
exit 0
""",
    )


@pytest.mark.parametrize(
    "tmux_args",
    [
        '-S "$SOCK.other" new-session -d -s other sh -c \'exec sleep 300\' sh "$SOCK"',
        '-S "$SOCK.other" new-session -d -s other sh -c \'exec sleep 300\' sh -S "$SOCK"',
        '-S "$SOCK.other" new-session -d -s "$SOCK" \'exec sleep 300\'',
        '-S "$SOCK" -S "$SOCK.other" new-session -d -s other \'exec sleep 300\'',
    ],
    ids=["path-in-pane-command", "socket-option-in-pane-command", "path-as-session-name", "overridden-socket-option"],
)
def test_is_our_server_rejects_a_tmux_server_whose_command_only_mentions_the_socket(tmux_args):
    # A first pane running a nested `tmux -S $SOCK attach` puts the path in the server's command line too, and
    # of two -S options tmux uses the last.
    run_with_staymux(
        f"tmux -f /dev/null {tmux_args} || exit 1\n"
        + r"""read -r server pane < <(tmux -S "$SOCK.other" display-message -p '#{pid} #{pane_pid}')
[[ -n $server && -n $pane ]] || exit 1
cleanup_pids+=("$server" "$pane")
if is_our_server "$server"; then
    printf 'accepted server %s whose command only mentions %s\n' "$server" "$SOCK"
    exit 1
fi
exit 0
""",
    )


@pytest.mark.parametrize(
    ("title", "status"),
    [("tmux: server ($SOCK)", 0), ("tmux: server ($SOCK.other)", 1), ("tmux: server (/elsewhere) ($SOCK)", 1)],
    ids=["own-socket", "another-socket", "socket-only-mentioned"],
)
def test_is_our_server_reads_a_title_that_replaced_the_command_line(title, status):
    # Builds whose setproctitle rewrites the command line keep the comm "tmux" and show only the title.
    run_with_staymux(
        f'title="{title}"\n'
        + r"""bash -c 'exec -a "$1" python3 -c "$2"' _ "$title" 'import ctypes, signal
ctypes.CDLL(None).prctl(15, b"tmux", 0, 0, 0)
print("ready", flush=True)
signal.pause()' >"$SOCK.ready" &
stand_in=$!
cleanup_pids+=("$stand_in")
for ((i = 0; i < 100; i++)); do
    [[ ! -s $SOCK.ready ]] || break
    kill -0 "$stand_in" 2>/dev/null || exit 1
    sleep 0.01
done
[[ -s $SOCK.ready ]] || exit 1
is_our_server "$stand_in"
status=$?
"""
        + f'[[ $status == {status} ]] || {{ printf "status %s for title %s\\n" "$status" "$title"; exit 1; }}\n'
        + "exit 0\n",
    )


@pytest.mark.parametrize(
    "tmux_args",
    ['-f/dev/null -uS"$SOCK"', '-f /dev/null -S "$SOCK.other" -S "$SOCK"'],
    ids=["clustered-options", "last-socket-option"],
)
def test_revive_server_restores_a_server_whose_own_socket_option_names_ours(tmux_args):
    run_with_staymux(
        f"tmux {tmux_args} new-session -d -s recovery 'exec sleep 300' || exit 1\n"
        + r"""read -r server pane < <(tmux -S "$SOCK" display-message -p '#{pid} #{pane_pid}')
[[ -n $server && -n $pane ]] || exit 1
cleanup_pids+=("$server" "$pane")
rm -- "$SOCK" || exit 1
revive_server || { printf 'did not revive server %s\n' "$server"; exit 1; }
[[ $REVIVED == "$server" ]] || exit 1
wait_for_socket || exit 1
tmux -S "$SOCK" has-session -t recovery || exit 1
exit 0
""",
    )


def test_holds_socket_accepts_a_real_tmux_server_before_and_after_unlink():
    run_with_staymux(
        r"""start_tmux_server || exit 1
holds_socket "$server" || exit 1
rm -- "$SOCK" || exit 1
holds_socket "$server" || exit 1
exit 0
""",
    )


def test_revive_server_with_mixed_holders_restores_only_tmux():
    run_with_staymux(
        START_NON_TMUX_LISTENER
        + r"""rm -- "$SOCK" || exit 1
start_tmux_server || exit 1
rm -- "$SOCK" || exit 1
revive_server || exit 1
[[ $REVIVED == "$server" ]] || exit 1
wait_for_socket || exit 1
tmux -S "$SOCK" has-session -t recovery || exit 1
kill -0 "$holder" 2>/dev/null || exit 1
exit 0
""",
    )


@pytest.mark.skipif(tmux_version() < (3, 2), reason="tmux -D needs tmux 3.2 or later")
def test_revive_server_is_not_hidden_by_a_non_tmux_parent_holder():
    run_with_staymux(
        r"""python3 - "$SOCK" >"$SOCK.ready" <<'PYTHON' &
import os
import socket
import subprocess
import sys

listener = socket.socket(socket.AF_UNIX)
listener.bind(sys.argv[1])
listener.listen()
os.unlink(sys.argv[1])
server = subprocess.Popen(["tmux", "-D", "-f", "/dev/null", "-S", sys.argv[1]])
print(server.pid, flush=True)
server.wait()
PYTHON
holder=$!
cleanup_pids+=("$holder")
for ((i = 0; i < 100; i++)); do
    [[ ! -s $SOCK.ready ]] || break
    kill -0 "$holder" 2>/dev/null || exit 1
    sleep 0.01
done
read -r server <"$SOCK.ready" || exit 1
cleanup_pids+=("$server")
wait_for_socket || exit 1
tmux -S "$SOCK" new-session -d -s recovery 'exec sleep 300' || exit 1
pane=$(tmux -S "$SOCK" display-message -p '#{pane_pid}')
cleanup_pids+=("$pane")
rm -- "$SOCK" || exit 1
revive_server || { printf 'parent holder hid server %s\n' "$server"; exit 1; }
[[ $REVIVED == "$server" ]] || exit 1
wait_for_socket || exit 1
tmux -S "$SOCK" has-session -t recovery || exit 1
kill -0 "$holder" 2>/dev/null || exit 1
exit 0
""",
    )


def test_revive_server_passes_over_a_server_child_that_still_holds_the_socket():
    # A child that a server has just forked shares its title, command line and socket. Stopped, it would come
    # first among the candidates, so only the rule that leaves out children of an accepted server saves it.
    # The stand-in server's command line, python3 -c CODE -S $SOCK, reads as tmux options too: -c takes a value.
    run_with_staymux(
        r"""stand_in=$(cat <<'PYTHON'
import ctypes
import os
import signal
import socket
import sys

ctypes.CDLL(None).prctl(15, b"tmux: server", 0, 0, 0)
signal.signal(signal.SIGUSR1, signal.SIG_IGN)
listener = socket.socket(socket.AF_UNIX)
listener.bind(sys.argv[2])
listener.listen()
os.unlink(sys.argv[2])
child = os.fork()
if child == 0:
    os.kill(os.getpid(), signal.SIGSTOP)
    os._exit(0)
os.waitpid(child, os.WUNTRACED)
print(child, flush=True)
signal.pause()
PYTHON
)
python3 -c "$stand_in" -S "$SOCK" >"$SOCK.ready" &
server=$!
cleanup_pids+=("$server")
for ((i = 0; i < 100; i++)); do
    [[ ! -s $SOCK.ready ]] || break
    kill -0 "$server" 2>/dev/null || exit 1
    sleep 0.01
done
read -r child <"$SOCK.ready" || exit 1
cleanup_pids+=("$child")
is_our_server "$child" || exit 1
revive_server || exit 1
[[ $REVIVED == "$server" ]] || { printf 'signalled %s, not server %s\n' "$REVIVED" "$server"; exit 1; }
exit 0
""",
    )
