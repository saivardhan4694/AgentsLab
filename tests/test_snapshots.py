import pytest

from agentlab.gateway.snapshots import SnapshotError, SnapshotStore


@pytest.fixture
def store(agentlab_home):
    return SnapshotStore(agentlab_home)


def test_file_change_is_rolled_back(store, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("v1", encoding="utf-8")
    snap = store.take("s1", "fs__write_file", {"path": str(f)}, [f])
    f.write_text("v2", encoding="utf-8")
    assert store.rollback(snap) == [str(f.resolve())]
    assert f.read_text(encoding="utf-8") == "v1"
    with pytest.raises(SnapshotError, match="already"):
        store.rollback(snap)


def test_new_path_is_removed_on_rollback(store, tmp_path):
    d = tmp_path / "new"
    snap = store.take("s1", "fs__make_dir", {"path": str(d)}, [d])
    d.mkdir()
    (d / "x.txt").write_text("x", encoding="utf-8")
    store.rollback(snap)
    assert not d.exists()


def test_deleted_tree_is_restored(store, tmp_path):
    d = tmp_path / "tree"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "a.txt").write_text("a", encoding="utf-8")
    (d / "empty").mkdir()
    snap = store.take("s1", "fs__delete", {"path": str(d)}, [d])
    import shutil
    shutil.rmtree(d)
    store.rollback(snap)
    assert (d / "sub" / "a.txt").read_text(encoding="utf-8") == "a"
    assert (d / "empty").is_dir()


def test_move_is_undone(store, tmp_path):
    src, dst = tmp_path / "a.txt", tmp_path / "b.txt"
    src.write_text("a", encoding="utf-8")
    snap = store.take("s1", "fs__move", {}, [src, dst])
    src.rename(dst)
    store.rollback(snap)
    assert src.read_text(encoding="utf-8") == "a"
    assert not dst.exists()


def test_session_rollback_goes_newest_first(store, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("v1", encoding="utf-8")
    store.take("s1", "w", {}, [f])
    f.write_text("v2", encoding="utf-8")
    store.take("s1", "w", {}, [f])
    f.write_text("v3", encoding="utf-8")
    other = store.take("s2", "w", {}, [tmp_path / "other.txt"])
    assert len(store.rollback_session("s1")) == 2
    assert f.read_text(encoding="utf-8") == "v1"
    assert store.get(other).rolled_back_at is None


def test_limits(agentlab_home, tmp_path):
    store = SnapshotStore(agentlab_home, max_bytes=10)
    f = tmp_path / "big.txt"
    f.write_text("x" * 100, encoding="utf-8")
    with pytest.raises(SnapshotError, match="Too much"):
        store.take("s1", "w", {}, [f])


def test_identical_content_is_stored_once(store, tmp_path):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("same", encoding="utf-8")
    store.take("s1", "w", {}, [tmp_path / "a.txt", tmp_path / "b.txt"])
    assert len([p for p in store.blobs.rglob("*") if p.is_file()]) == 1
