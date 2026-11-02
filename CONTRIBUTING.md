# Contributing

Issues and pull requests are welcome, and reviewed when time allows.

## Testing

The tests need bash, tmux, Python 3 with pytest, bubblewrap, and `unshare` from util-linux. bubblewrap needs unprivileged user namespaces, which Ubuntu 24.04 and later restrict through AppArmor. There, allow them for `bwrap` alone with this profile in `/etc/apparmor.d/bwrap`, loaded with `sudo apparmor_parser -r /etc/apparmor.d/bwrap`. Any program can then create user namespaces through `bwrap`:

```
abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
}
```

CI instead lifts the restriction for every program until the next boot (`sysctl kernel.apparmor_restrict_unprivileged_userns=0`); keep that to throwaway machines.

```
./run-tests                                 # every test
./run-tests tests/test_start.py -k slice    # arguments go to pytest; paths are relative to the checkout
./run-tests -- bash -c '...'                # any other command, such as a probe, in the same sandbox
shellcheck staymux
```

`run-tests` sandboxes the tests with bubblewrap, and pytest refuses to run without it. Run a new fixture's first try, or any experiment with one, through it too. Set `STAYMUX_NO_SKIPS=1` to fail on any skipped test, as CI does.

The tests stub systemd. A change to how staymux works with logind, lingering or scopes needs a try on a disposable VM too; say in the pull request what you ran there and what you saw.

## Releasing

Versions follow MAJOR.MINOR.PATCH: MAJOR for changes that break existing use or leave running servers behind, MINOR for new features, PATCH for fixes. A pull request that changes what users see adds a line to CHANGELOG.md under an `## Unreleased` heading at the top, starting that heading if it isn't there yet.

1. In a pull request, rename `## Unreleased` to the new version and set `VERSION` in `staymux` to match.
2. Once it's merged, tag the merge commit `vX.Y.Z` and push the tag.
