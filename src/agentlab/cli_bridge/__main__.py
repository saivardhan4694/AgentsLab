"""CLI bridge commands.

    python -m agentlab.cli_bridge serve --manifests config/tools [--root DIR ...]   # MCP server (stdio)
    python -m agentlab.cli_bridge list --manifests config/tools                     # check manifests
    python -m agentlab.cli_bridge discover [--write config/tools]                   # find known CLIs
    python -m agentlab.cli_bridge draft <program> --write config/tools              # skeleton manifest
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import anyio
import yaml
from mcp.server.stdio import stdio_server

from agentlab.cli_bridge.manifest import ManifestError, load_manifest, load_manifests, resolve_binary
from agentlab.cli_bridge.server import create_server

TEMPLATES = Path(__file__).parent / "templates"

DRAFT = """# Draft manifest for {binary}. Review every line before use: the tools below are guesses.
# Schema: src/agentlab/cli_bridge/manifest.py. Check it with: python -m agentlab.cli_bridge list
#
# {help}
name: {name}
binary: {binary_yaml}
description: {summary}
tools:
  - name: version
    description: Show the installed version of {binary}
    risk: low
    timeout_s: 30
    argv: ["--version"]
  # Add one tool per task. Keep options fixed in argv and expose only the values the model needs:
  # - name: convert
  #   description: ...
  #   risk: medium
  #   args:
  #     input:  {{ type: path, must_exist: true }}
  #     output: {{ type: path }}
  #   argv: ["{{input}}", "-o", "{{output}}"]
  #   writes: [output]
"""


async def serve(manifests: Path, roots: list[Path]) -> None:
    tools, notes = load_manifests(manifests)
    for n in notes:
        print(n, file=sys.stderr)
    server = create_server(tools, roots)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def cmd_list(manifests: Path) -> None:
    for path in sorted(manifests.glob("*.yaml")):
        try:
            tools = load_manifest(path)
        except ManifestError as e:
            print(f"{path.name}: ERROR {e}")
            continue
        if not tools:
            print(f"{path.name}: binary not found, skipped")
        for t in tools:
            args = ", ".join(f"{n}:{a.type}" + ("" if a.required and a.default is None else "?") for n, a in t.args.items())
            print(f"{t.name:<22} risk={t.risk:<8} ({args})" + (f"  writes={t.writes}" if t.writes else ""))


def cmd_discover(write: Path | None) -> None:
    installed = {p.name for p in write.glob("*.yaml")} if write else set()
    print(f"{'template':<12} {'program':<10} found at")
    for template in sorted(TEMPLATES.glob("*.yaml")):
        binary = yaml.safe_load(template.read_text(encoding="utf-8"))["binary"]
        found = resolve_binary(binary)
        status = str(found) if found else "-"
        if found and write and template.name not in installed:
            write.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(template, write / template.name)
            status += f"  -> added {write / template.name}"
        elif found and template.name in installed:
            status += "  (already in manifests)"
        print(f"{template.stem:<12} {binary:<10} {status}")
    if not write:
        print("\nAdd the found ones with --write config/tools, then restart the Gateway.")


def cmd_draft(program: str, write: Path) -> None:
    binary = resolve_binary(program)
    if binary is None:
        sys.exit(f"{program} is not on PATH")
    try:
        out = subprocess.run([str(binary), "--help"], capture_output=True, text=True, timeout=10,
                             stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    name = "".join(c if c.isalnum() or c in "_-" else "_" for c in Path(program).stem)
    target = write / f"{name}.yaml"
    if target.exists():
        sys.exit(f"{target} already exists")
    write.mkdir(parents=True, exist_ok=True)
    target.write_text(DRAFT.format(
        binary=program, binary_yaml=json.dumps(program), name=name,
        summary=json.dumps(lines[0][:100] if lines else program),
        help=(lines[0][:150] if lines else "no --help output")), encoding="utf-8")
    print(f"Wrote {target}. Edit it, then check it with: python -m agentlab.cli_bridge list --manifests {write}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m agentlab.cli_bridge", description="Expose installed CLIs as MCP tools.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--manifests", type=Path, default=Path("config/tools"))
    p.add_argument("--root", type=Path, action="append", default=[], help="allowed folder for paths; repeatable")
    p = sub.add_parser("list")
    p.add_argument("--manifests", type=Path, default=Path("config/tools"))
    p = sub.add_parser("discover")
    p.add_argument("--write", type=Path, help="manifests folder to copy found templates into")
    p = sub.add_parser("draft")
    p.add_argument("program")
    p.add_argument("--write", type=Path, default=Path("config/tools"))
    a = parser.parse_args()

    if a.command == "serve":
        anyio.run(serve, a.manifests, a.root)
    elif a.command == "list":
        cmd_list(a.manifests)
    elif a.command == "discover":
        cmd_discover(a.write)
    else:
        cmd_draft(a.program, a.write)


if __name__ == "__main__":
    main()
