"""CI must never turn missing PostgreSQL into a successful billing test gate."""

import pytest


def pytest_addoption(parser):
    parser.addoption('--require-db', action='store_true', help='Fail the gate if any test skips.')


def pytest_sessionfinish(session, exitstatus):
    if session.config.getoption('--require-db'):
        reporter = session.config.pluginmanager.get_plugin('terminalreporter')
        if reporter and reporter.stats.get('skipped'):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
