"""Shell MCP server: run one command and return its output.

Safety layers in this server (the Gateway policy comes on top):

- Working-directory jail: `cwd` must resolve inside one of the `--root` folders.
  This limits where a command starts, not what it can touch. A command can still `cd` anywhere.
- Timeout, then the whole process tree is killed.
- Output cap per stream.
- Minimal environment: only a short list of variables, plus any passed with `--pass-env`.
- No stdin: stdin carries the MCP protocol, so the child gets an empty stdin.

This is not a sandbox. Treat `run_command` as critical risk.

Run standalone:  python -m agentlab.servers.shell --root ~/agentlab-sandbox
"""

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from agentlab.shared.proc import minimal_env, run_process

ROOTS_ENV = "AGENTLAB_SHELL_ROOTS"
DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 600

SHELL = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)


def shell_argv(shell: str, command: str) -> list[str]:
    if shell == "powershell":
        exe = shutil.which("pwsh") or shutil.which("powershell")
        if exe is None:
            raise ToolError("PowerShell is not installed")
        return [exe, "-NoProfile", "-NonInteractive", "-Command", command]
    if shell == "cmd":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]
    if shell == "bash":
        exe = shutil.which("bash")
        if exe is None:
            raise ToolError("bash is not installed")
        return [exe, "-c", command]
    raise ToolError(f"Unknown shell {shell!r}; use powershell, cmd, or bash")


def create_server(roots: list[Path] | None = None, pass_env: list[str] | None = None) -> MCPServer:
    roots = [r.expanduser().resolve() for r in roots or []]
    default_shell = "powershell" if sys.platform == "win32" else "bash"

    def resolve_cwd(cwd: str | None) -> Path:
        if cwd is None:
            p = roots[0] if roots else Path.home()
        else:
            p = Path(cwd).expanduser().resolve()
        if roots and not any(p == r or p.is_relative_to(r) for r in roots):
            raise ToolError(f"cwd is outside the allowed roots: {p}")
        if not p.is_dir():
            raise ToolError(f"cwd is not a directory: {p}")
        return p

    roots_note = ", ".join(map(str, roots)) if roots else "unrestricted"
    server = MCPServer(
        "agentlab-shell",
        version="0.1.0",
        instructions=f"Runs shell commands. Default shell: {default_shell}. Allowed working directories: {roots_note}.",
    )

    @server.tool(annotations=SHELL)
    async def run_command(
        command: str,
        cwd: str | None = None,
        shell: str = default_shell,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Run a shell command and return exit code, stdout, and stderr.

        shell is powershell, cmd, or bash. timeout_s is capped at 600; on timeout the
        process tree is killed. Output is capped at 100 KB per stream.
        """
        workdir = resolve_cwd(cwd)
        timeout_s = max(1, min(timeout_s, MAX_TIMEOUT_S))
        return await run_process(shell_argv(shell, command), workdir, minimal_env(pass_env), timeout_s)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentLab shell MCP server (stdio)")
    parser.add_argument("--root", action="append", default=[], help="allowed working directory; repeatable")
    parser.add_argument("--pass-env", action="append", default=[], help="extra environment variable to pass; repeatable")
    args = parser.parse_args()
    roots = args.root or [r for r in os.environ.get(ROOTS_ENV, "").split(os.pathsep) if r]
    create_server([Path(r) for r in roots], args.pass_env).run()


if __name__ == "__main__":
    main()
