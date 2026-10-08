import inspect

import pytest


@pytest.fixture(autouse=True)
def agentlab_home(tmp_path, monkeypatch):
    # Keep snapshots and approvals out of the real ~/.agentlab.
    home = tmp_path / "agentlab-home"
    monkeypatch.setenv("AGENTLAB_HOME", str(home))
    return home


@pytest.fixture
def anyio_backend():
    return "asyncio"


def pytest_collection_modifyitems(items):
    # Run every async test with anyio's plugin. It keeps fixture setup, test, and teardown
    # in one task, which the MCP SDK's cancel scopes require.
    for item in items:
        if inspect.iscoroutinefunction(getattr(item, "function", None)):
            item.add_marker(pytest.mark.anyio)
