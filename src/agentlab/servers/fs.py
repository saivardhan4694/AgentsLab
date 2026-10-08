"""Filesystem MCP server.

Access policy belongs to the Gateway. This server adds one independent safety layer:
optional root directories. When roots are set, every path must resolve inside one of them.

Run standalone:  uv run agentlab-fs --root ~/sandbox
"""

import argparse
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

ROOTS_ENV = "AGENTLAB_FS_ROOTS"
MAX_READ_BYTES = 1_000_000
MAX_SEARCH_RESULTS = 1_000

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _entry(path: Path) -> dict[str, Any]:
    st = path.stat()
    return {
        "name": path.name,
        "path": str(path),
        "type": "dir" if path.is_dir() else "file",
        "size": st.st_size,
        "modified": _iso(st.st_mtime),
    }


def create_server(roots: list[Path] | None = None) -> MCPServer:
    roots = [r.expanduser().resolve() for r in roots or []]

    def resolve(path: str) -> Path:
        # Resolve "~", "..", and symlinks before the root check, so "root/../secret" cannot escape.
        p = Path(path).expanduser().resolve()
        if roots and not any(p == r or p.is_relative_to(r) for r in roots):
            raise ToolError(f"Path is outside the allowed roots: {p}")
        return p

    def require_exists(p: Path) -> None:
        if not p.exists():
            raise ToolError(f"Path does not exist: {p}")

    roots_note = ", ".join(map(str, roots)) if roots else "unrestricted"
    server = MCPServer(
        "agentlab-fs",
        version="0.1.0",
        instructions=f"Read and change files on the local machine. Allowed roots: {roots_note}.",
    )

    @server.tool(annotations=READ_ONLY)
    def list_dir(path: str, show_hidden: bool = False) -> dict[str, Any]:
        """List the files and folders in a directory."""
        p = resolve(path)
        require_exists(p)
        if not p.is_dir():
            raise ToolError(f"Not a directory: {p}")
        entries = []
        for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
            if not show_hidden and child.name.startswith("."):
                continue
            try:
                entries.append(_entry(child))
            except OSError:
                continue  # broken symlink or no permission
        return {"path": str(p), "entries": entries}

    @server.tool(annotations=READ_ONLY)
    def read_file(path: str, max_bytes: int = 200_000) -> dict[str, Any]:
        """Read a text file. Output is truncated to max_bytes. Invalid UTF-8 bytes are replaced."""
        p = resolve(path)
        require_exists(p)
        if not p.is_file():
            raise ToolError(f"Not a file: {p}")
        max_bytes = max(1, min(max_bytes, MAX_READ_BYTES))
        size = p.stat().st_size
        with p.open("rb") as f:
            data = f.read(max_bytes)
        return {
            "path": str(p),
            "content": data.decode("utf-8", errors="replace"),
            "size": size,
            "truncated": size > max_bytes,
        }

    @server.tool(annotations=READ_ONLY)
    def stat(path: str) -> dict[str, Any]:
        """Get metadata for a file or directory."""
        p = resolve(path)
        require_exists(p)
        st = p.stat()
        return {**_entry(p), "created": _iso(st.st_ctime), "is_symlink": p.is_symlink()}

    @server.tool(annotations=READ_ONLY)
    def search(path: str, pattern: str, max_results: int = 200) -> dict[str, Any]:
        """Find files and folders under a directory whose name matches a glob pattern, for example '*.py'."""
        p = resolve(path)
        require_exists(p)
        max_results = max(1, min(max_results, MAX_SEARCH_RESULTS))
        matches = []
        for match in p.rglob(pattern):
            if roots and not any(match.resolve().is_relative_to(r) for r in roots):
                continue  # symlink pointing outside the roots
            matches.append(str(match))
            if len(matches) >= max_results:
                return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    @server.tool(annotations=WRITE)
    def write_file(path: str, content: str, overwrite: bool = False, create_dirs: bool = False) -> dict[str, Any]:
        """Write text to a file. Fails if the file exists, unless overwrite is true."""
        p = resolve(path)
        if p.is_dir():
            raise ToolError(f"Path is a directory: {p}")
        if p.exists() and not overwrite:
            raise ToolError(f"File exists (set overwrite=true to replace it): {p}")
        if not p.parent.exists():
            if not create_dirs:
                raise ToolError(f"Parent directory does not exist (set create_dirs=true): {p.parent}")
            p.parent.mkdir(parents=True)
        p.write_text(content, encoding="utf-8")
        return {"path": str(p), "bytes_written": len(content.encode("utf-8"))}

    @server.tool(annotations=WRITE)
    def make_dir(path: str) -> dict[str, Any]:
        """Create a directory, including missing parent directories."""
        p = resolve(path)
        if p.exists() and not p.is_dir():
            raise ToolError(f"A file with this name exists: {p}")
        p.mkdir(parents=True, exist_ok=True)
        return {"path": str(p)}

    @server.tool(annotations=DESTRUCTIVE)
    def move(source: str, destination: str, overwrite: bool = False) -> dict[str, Any]:
        """Move or rename a file or directory."""
        src, dst = resolve(source), resolve(destination)
        require_exists(src)
        if dst.exists():
            if not overwrite:
                raise ToolError(f"Destination exists (set overwrite=true to replace it): {dst}")
            if dst.is_dir() != src.is_dir():
                raise ToolError("Cannot overwrite a file with a directory, or a directory with a file")
            shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
        shutil.move(src, dst)
        return {"source": str(src), "destination": str(dst)}

    @server.tool(annotations=DESTRUCTIVE)
    def delete(path: str, recursive: bool = False) -> dict[str, Any]:
        """Delete a file, or a directory. A non-empty directory needs recursive=true."""
        p = resolve(path)
        require_exists(p)
        if p in roots:
            raise ToolError("Refusing to delete an allowed root directory")
        if p.is_dir():
            if recursive:
                shutil.rmtree(p)
            elif any(p.iterdir()):
                raise ToolError(f"Directory is not empty (set recursive=true): {p}")
            else:
                p.rmdir()
        else:
            p.unlink()
        return {"deleted": str(p)}

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentLab filesystem MCP server (stdio)")
    parser.add_argument("--root", action="append", default=[], help="allowed root directory; repeatable")
    args = parser.parse_args()
    roots = args.root or [r for r in os.environ.get(ROOTS_ENV, "").split(os.pathsep) if r]
    create_server([Path(r) for r in roots]).run()


if __name__ == "__main__":
    main()
