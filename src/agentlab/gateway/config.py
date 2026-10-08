"""Load `servers.yaml` into registry entries.

```yaml
servers:
  fs:
    command: uv
    args: [run, agentlab-fs, --root, ~/sandbox]
    env: { SOME_VAR: value }   # added on top of the SDK's safe default environment
    cwd: ..                    # relative paths resolve against this file's folder
    timeout: 30                # seconds per call
    risk: { delete: critical } # tool glob -> low|medium|high|critical; overrides annotations
    writes:                    # tool -> path arguments it changes; the Gateway snapshots them
      write_file: [path]
    trust_meta: false          # true only for first-party servers: read risk and writes from tool _meta
    enabled: true
```

`${VAR}` in args and env values expands from the Gateway's environment, so secrets stay out of the file.
"""

import os
from pathlib import Path
from typing import Any

import yaml
from mcp import StdioServerParameters

from agentlab.gateway.registry import ServerEntry


def load_servers(path: Path) -> list[ServerEntry]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    servers = data.get("servers") or {}
    if not isinstance(servers, dict):
        raise ValueError(f"{path}: 'servers' must be a mapping of name to settings")
    return [
        _entry(name, spec, path)
        for name, spec in servers.items()
        if spec.get("enabled", True)
    ]


def _entry(name: str, spec: dict[str, Any], path: Path) -> ServerEntry:
    if "command" not in spec:
        raise ValueError(f"{path}: server {name!r} needs a 'command'")
    cwd = spec.get("cwd")
    if cwd:
        cwd = (path.parent / Path(cwd).expanduser()).resolve()
    params = StdioServerParameters(
        command=spec["command"],
        args=[os.path.expandvars(str(a)) for a in spec.get("args", [])],
        env={k: os.path.expandvars(str(v)) for k, v in (spec.get("env") or {}).items()} or None,
        cwd=cwd,
    )
    return ServerEntry(name=name, target=params, timeout=spec.get("timeout"), risk=spec.get("risk") or {},
                       writes=spec.get("writes") or {}, trust_meta=bool(spec.get("trust_meta", False)))
