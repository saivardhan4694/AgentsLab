"""Run a child process safely from an MCP server: no stdin, minimal environment, timeout with
process-tree kill, and capped output."""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil

MAX_OUTPUT_BYTES = 100_000

# Variables a normal program needs to start. Everything else (tokens, keys) stays out.
BASE_ENV = [
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM", "TZ",
]


def minimal_env(extra: list[str] | None = None) -> dict[str, str]:
    return {k: os.environ[k] for k in [*BASE_ENV, *(extra or [])] if k in os.environ}


def kill_tree(pid: int) -> None:
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    for p in [*parent.children(recursive=True), parent]:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass


async def read_capped(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    # Keep reading after the cap, so a chatty process does not block on a full pipe.
    data, truncated = bytearray(), False
    while chunk := await stream.read(65536):
        room = limit - len(data)
        if room > 0:
            data += chunk[:room]
        if len(chunk) > room:
            truncated = True
    return bytes(data), truncated


async def run_process(argv: list[str], cwd: Path, env: dict[str, str], timeout_s: float,
                      max_output: int = MAX_OUTPUT_BYTES) -> dict[str, Any]:
    """Run argv (never through a shell) and return exit code, output, and timing."""
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,  # stdin carries the MCP protocol; never share it
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=flags,
        start_new_session=sys.platform != "win32",
    )
    assert proc.stdout is not None and proc.stderr is not None
    readers = asyncio.gather(read_capped(proc.stdout, max_output), read_capped(proc.stderr, max_output))
    timed_out = False
    try:
        (out, out_cut), (err, err_cut) = await asyncio.wait_for(asyncio.shield(readers), timeout_s)
        await proc.wait()
    except TimeoutError:
        timed_out = True
        kill_tree(proc.pid)
        await proc.wait()
        try:
            (out, out_cut), (err, err_cut) = await asyncio.wait_for(readers, 5)
        except TimeoutError:  # a detached grandchild still holds the pipe
            (out, out_cut), (err, err_cut) = (b"", True), (b"", True)
    return {
        "exit_code": proc.returncode,
        "stdout": out.decode("utf-8", errors="replace"),
        "stderr": err.decode("utf-8", errors="replace"),
        "timed_out": timed_out,
        "truncated": out_cut or err_cut,
        "duration_ms": round((time.monotonic() - start) * 1000),
        "cwd": str(cwd),
    }
