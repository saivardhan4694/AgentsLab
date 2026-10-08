import pytest
from mcp import Client

from agentlab.servers.fs import create_server


@pytest.fixture
async def fs(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "docs" / "b.py").write_text("print(1)", encoding="utf-8")
    async with Client(create_server(roots=[tmp_path])) as client:
        yield client, tmp_path


async def call(client, name, **args):
    return await client.call_tool(name, args)


async def test_tools_have_annotations(fs):
    client, _ = fs
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"list_dir", "read_file", "stat", "search", "write_file", "make_dir", "move", "delete"}
    assert tools["read_file"].annotations.read_only_hint is True
    assert tools["delete"].annotations.destructive_hint is True


async def test_list_and_read(fs):
    client, root = fs
    listing = await call(client, "list_dir", path=str(root / "docs"))
    assert [e["name"] for e in listing.structured_content["entries"]] == ["a.txt", "b.py"]
    read = await call(client, "read_file", path=str(root / "docs" / "a.txt"))
    assert read.structured_content["content"] == "hello"


async def test_read_truncates(fs):
    client, root = fs
    read = await call(client, "read_file", path=str(root / "docs" / "a.txt"), max_bytes=2)
    assert read.structured_content["content"] == "he"
    assert read.structured_content["truncated"] is True


async def test_escape_outside_root_is_blocked(fs):
    client, root = fs
    result = await call(client, "read_file", path=str(root / "docs" / ".." / ".." / "secret.txt"))
    assert result.is_error
    assert "outside the allowed roots" in result.content[0].text


async def test_write_refuses_overwrite_by_default(fs):
    client, root = fs
    target = root / "docs" / "a.txt"
    result = await call(client, "write_file", path=str(target), content="new")
    assert result.is_error
    assert target.read_text(encoding="utf-8") == "hello"
    result = await call(client, "write_file", path=str(target), content="new", overwrite=True)
    assert not result.is_error
    assert target.read_text(encoding="utf-8") == "new"


async def test_write_create_dirs(fs):
    client, root = fs
    target = root / "x" / "y" / "z.txt"
    assert (await call(client, "write_file", path=str(target), content="z")).is_error
    assert not (await call(client, "write_file", path=str(target), content="z", create_dirs=True)).is_error
    assert target.read_text(encoding="utf-8") == "z"


async def test_search(fs):
    client, root = fs
    result = await call(client, "search", path=str(root), pattern="*.py")
    assert result.structured_content["matches"] == [str(root / "docs" / "b.py")]


async def test_move_and_delete(fs):
    client, root = fs
    src, dst = root / "docs" / "a.txt", root / "docs" / "c.txt"
    assert not (await call(client, "move", source=str(src), destination=str(dst))).is_error
    assert dst.exists() and not src.exists()

    assert (await call(client, "delete", path=str(root / "docs"))).is_error  # not empty
    assert not (await call(client, "delete", path=str(root / "docs"), recursive=True)).is_error
    assert not (root / "docs").exists()


async def test_cannot_delete_root(fs):
    client, root = fs
    result = await call(client, "delete", path=str(root), recursive=True)
    assert result.is_error
    assert root.exists()
