"""Refuse to run outside ./run-tests, whose sandbox keeps a test from changing your files and hides the usual
locations of your session's services.

It sits at the checkout's root, which pytest.ini makes pytest's rootdir, so pytest imports it before any other
conftest.py here, and it refuses while it is imported, before any of them can run. With STAYMUX_NO_SKIPS=1, as CI
sets it, a skipped test also fails the run.
"""

import os
import sys

import pytest

if os.environ.get("STAYMUX_TEST_SANDBOX") != "1":
    # SystemExit, since pytest.exit here would be reported as a broken conftest with a traceback. The message still
    # comes first, though pytest 7.4 adds a warning from pluggy about the exit below it.
    print("run the tests with ./run-tests, which sandboxes them", file=sys.stderr)
    raise SystemExit(pytest.ExitCode.USAGE_ERROR)

def skips(config):
    """The tests skipped so far, when STAYMUX_NO_SKIPS=1 asks for none, as pytest's own summary counts them."""
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if os.environ.get("STAYMUX_NO_SKIPS") != "1" or reporter is None:
        return []
    return reporter.stats.get("skipped", [])


def pytest_sessionfinish(session, exitstatus):
    if exitstatus == pytest.ExitCode.OK and skips(session.config):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if skips(config):
        terminalreporter.write_sep("=", "STAYMUX_NO_SKIPS=1 fails the run on any skipped test", red=True)
