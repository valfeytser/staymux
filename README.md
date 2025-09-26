# staymux

`staymux` keeps your `tmux` sessions alive by launching the server inside a user-scoped `systemd` slice and enabling linger automatically. It is designed for secure, compliance-bound environments where idle shells are terminated aggressively, yet administrators still need reliable, long-lived terminal workflows.

## Why This Matters in STIGed Environments

DoD STIG guidance for Red Hat Enterprise Linux 8.7 and later (for example, [RHEL-08-020035](https://public.cyber.mil/stigs/downloads/), also referenced as V-257258 / SV-257258r1069328) mandates that inactive interactive sessions be closed after ten minutes:

> "Configure RHEL 8 to log out idle sessions after 10 minutes... StopIdleSessionSec=600." (RHEL 8 STIG, Version 2 Release 4, 02 Jul 2025)

That policy is essential for limiting unattended access, but it also means a long-running task can drop midstream if the operator steps away. With `staymux`, an administrator can launch a Satellite upgrade or other extended maintenance, detach from tmux, log out, and keep working elsewhere. If an SSH session is terminated by the ten-minute idle timeout, the tmux session stays intact so the job continues safely in the background.

## Requirements

- **Operating System**: Modern Linux distribution with systemd support
  - Ubuntu 18.04 LTS or later
  - RHEL/CentOS/Rocky/AlmaLinux 8.0 or later
- **systemd**: Version 230 or later (for reliable user scope support)
- **tmux**: Version 2.1 or later

## Features

- Starts `tmux` in a dedicated scope (`stmux-<user>.scope`) and keeps it alive after logout.
- Enables linger for the current account on first run, so the user instance continues running out of band.
- Provides guard rails for root execution and nested `tmux` sessions.
- Automatic OS and dependency version checking on startup.
- Friendly helper output, including usage examples and primary key bindings.

## Quick Start

1. Copy `staymux` into a directory on your `$PATH`, e.g. `/usr/local/bin/staymux`.
2. Ensure it is executable: `chmod 755 /usr/local/bin/staymux`.
3. Run `staymux` once. If necessary, it will prompt you to enable linger (`loginctl enable-linger $USER`).
4. Disconnect from SSH or close your terminal; reconnect later and reattach with `staymux <session>`.

## Usage

```bash
staymux                # attach to the single active session or create one named after the host
staymux dev            # attach to (or create) a session named "dev"
staymux -l             # list sessions managed by the tmux server
staymux -k <session>   # kill a specific session
staymux -k all         # tear down the tmux server and scope entirely
```

If you run into a compliance lockout or an SSH idle timeout, re-run `staymux` and you can continue where you left off.

## Long Operations & Maintenance Windows

During activities such as:

- Red Hat Satellite upgrades that take hours to complete
- Bulk content deployments or long Ansible runs

`staymux` provides an anchored session that survives the idle timeout requirement. Administrators can step away, let cron jobs run, or reconnect after maintenance windows without losing state.

## Development Notes

- The script automatically checks OS and dependency versions on startup and will warn about unsupported configurations.
- Contributions are welcome via pull request.

### Version Support Notes

- Minimum requirements set to Ubuntu 18.04 LTS and RHEL 8
- Earlier versions may work but are not supported or tested

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
