# staymux

tmux sessions that survive logout, for unprivileged users on systemd hosts.

staymux runs one private tmux server per user in a scope of the user's own systemd manager, in `staymux.slice`, and turns on lingering for the account, so the server keeps running after every login has ended.

## Why

The RHEL 8 (8.7 and later) and RHEL 9 STIGs end a login left idle for 10 minutes and kill the programs it started (`StopIdleSessionSec=600`: RHEL-08-020035, RHEL-09-412080). The RHEL 10 (V1R2), Ubuntu 22.04 (V2R9) and Ubuntu 24.04 (V1R6) STIGs have no such rule. All five set `ClientAliveInterval 600` and `ClientAliveCountMax 1` for SSH, which drop a connection whose client stops answering, and with it the job running in that connection's shell. A job running in staymux survives both.

`TMOUT`, which these STIGs also set, still closes a shell left idle at its prompt, in staymux as anywhere else.

## Install

Needs bash 4.4+, tmux 2.7+, systemd 238+ (230+ on cgroup v1 hosts), util-linux and coreutils.

```
sudo install -m 755 staymux /usr/local/bin/staymux
```

## Use

```
staymux          attach to your session, creating one if there is none; lists them if there are several
staymux NAME     attach to NAME, creating it if needed
staymux -l       list sessions
staymux -k NAME  kill a session; -k all kills every session and the server
staymux -V       show the version
staymux -h       show help
```

## Contributing

Issues and pull requests are welcome, and reviewed when time allows. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[0BSD](LICENSE)

Written by Val Feytser with Claude (Anthropic) in Claude Code, and reviewed with GPT (OpenAI) in Codex.
