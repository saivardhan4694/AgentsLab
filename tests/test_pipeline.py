"""Approvals and snapshots through the full Gateway pipeline, with in-process servers."""

from functools import partial

import anyio
import pytest
from mcp import Client

from agentlab.gateway.approvals import ApprovalStore
from agentlab.gateway.pipeline import Gateway
from agentlab.gateway.policy.engine import PolicyEngine, Profile, Rule
from agentlab.gateway.registry import Registry, ServerEntry
from agentlab.gateway.server import create_server
from agentlab.gateway.snapshots import SnapshotStore
from agentlab.servers import fs

WRITES = {"write_file": ["path"], "make_dir": ["path"], "move": ["source", "destination"], "delete": ["path"]}


def profile(*rules):
    guard = Rule(match={"any_arg": "**/secret.txt"}, action="deny", source="guard", reason="secrets")
    return Profile("t", visible=["*"], visible_risk=None, guards=[guard], budgets=[],
                   rules=[Rule(match=m, action=a, source=f"r{i}") for i, (m, a) in enumerate(rules)])


@pytest.fixture
async def setup(tmp_path, agentlab_home):
    work = tmp_path / "work"
    work.mkdir()
    approvals, snapshots = ApprovalStore(agentlab_home), SnapshotStore(agentlab_home)

    async def make(*rules, timeout=5.0):
        gw = Gateway(registry, PolicyEngine(profile(*rules)), approvals, snapshots, approval_timeout=timeout)
        return gw, Client(create_server(gw))

    async with Registry([ServerEntry("fs", fs.create_server(roots=[work]), writes=WRITES)]) as registry:
        yield make, work, approvals, snapshots


async def answer_when_pending(approvals, status, **kwargs):
    while not (pending := approvals.pending()):
        await anyio.sleep(0.05)
    approvals.decide(pending[0].id, status, **kwargs)


async def test_snapshot_then_rollback(setup):
    make, work, _, snapshots = setup
    f = work / "a.txt"
    f.write_text("original", encoding="utf-8")
    gw, client = await make(({"tool": "fs__write_file"}, "allow_with_snapshot"))
    async with client:
        result = await client.call_tool("fs__write_file", {"path": str(f), "content": "changed", "overwrite": True})
    assert not result.is_error
    snap_id = result.meta["agentlab/snapshot"]
    assert f.read_text(encoding="utf-8") == "changed"
    snapshots.rollback(snap_id)
    assert f.read_text(encoding="utf-8") == "original"
    assert snapshots.get(snap_id).session_id == gw.session_id


async def test_snapshot_required_but_no_metadata(setup):
    make, work, _, _ = setup
    _, client = await make(({"tool": "fs__stat"}, "allow_with_snapshot"))
    async with client:
        result = await client.call_tool("fs__stat", {"path": str(work)})
    assert result.is_error and "writes" in result.content[0].text


async def test_approved_call_runs(setup):
    make, work, approvals, _ = setup
    f = work / "a.txt"
    f.write_text("x", encoding="utf-8")
    _, client = await make(({"tool": "fs__delete"}, "ask"))
    async with client, anyio.create_task_group() as tg:
        tg.start_soon(answer_when_pending, approvals, "approved")
        result = await client.call_tool("fs__delete", {"path": str(f)})
    assert not result.is_error
    assert not f.exists()
    assert "agentlab/snapshot" in result.meta  # deletes are snapshotted too


async def test_denied_call_does_not_run(setup):
    make, work, approvals, _ = setup
    f = work / "a.txt"
    f.write_text("x", encoding="utf-8")
    _, client = await make(({"tool": "fs__delete"}, "ask"))
    async with client, anyio.create_task_group() as tg:
        tg.start_soon(partial(answer_when_pending, approvals, "denied", reason="no way"))
        result = await client.call_tool("fs__delete", {"path": str(f)})
    assert result.is_error and "no way" in result.content[0].text
    assert f.exists()


async def test_unanswered_request_expires(setup):
    make, work, approvals, _ = setup
    _, client = await make(({"tool": "fs__delete"}, "ask"), timeout=0.5)
    async with client:
        result = await client.call_tool("fs__delete", {"path": str(work / "a.txt")})
    assert result.is_error and "expired" in result.content[0].text
    assert approvals.pending() == []


async def test_edited_args_must_pass_guards(setup):
    make, work, approvals, _ = setup
    (work / "secret.txt").write_text("hunter2", encoding="utf-8")
    _, client = await make(({"tool": "fs__read_file"}, "ask"))
    async with client, anyio.create_task_group() as tg:
        tg.start_soon(partial(answer_when_pending, approvals, "approved", args={"path": str(work / "secret.txt")}))
        result = await client.call_tool("fs__read_file", {"path": str(work / "other.txt")})
    assert result.is_error and "edited arguments" in result.content[0].text


async def test_edited_args_are_used(setup):
    make, work, approvals, _ = setup
    (work / "b.txt").write_text("bee", encoding="utf-8")
    _, client = await make(({"tool": "fs__read_file"}, "ask"))
    async with client, anyio.create_task_group() as tg:
        tg.start_soon(partial(answer_when_pending, approvals, "approved", args={"path": str(work / "b.txt")}))
        result = await client.call_tool("fs__read_file", {"path": str(work / "a.txt")})
    assert not result.is_error and "bee" in result.content[0].text


def test_answer_after_expiry_is_rejected(agentlab_home):
    store = ApprovalStore(agentlab_home)
    a = store.create("s", "fs__delete", {}, "high", "r")
    assert store.decide(a, "expired")
    assert not store.decide(a, "approved")
    assert store.get(a).status == "expired"


class RecordingPlugin:
    name = "recording"

    def __init__(self):
        self.seen: list[str] = []

    def before_call(self, call):
        self.seen.append(call.tool)
        return call

    def after_call(self, call, result):
        return result


async def test_plugins_only_see_calls_the_policy_allows(tmp_path, agentlab_home):
    work = tmp_path / "w"
    work.mkdir()
    plugin = RecordingPlugin()
    async with Registry([ServerEntry("fs", fs.create_server(roots=[work]))]) as registry:
        gw = Gateway(registry, PolicyEngine(profile(({"tool": "fs__stat"}, "allow"))), plugins=[plugin])
        async with Client(create_server(gw)) as client:
            await client.call_tool("fs__stat", {"path": str(work)})
            denied = await client.call_tool("fs__delete", {"path": str(work)})  # default deny
    assert denied.is_error
    assert plugin.seen == ["fs__stat"]
