"""CLI tool manifests: turn an installed command-line program into MCP tools with YAML, no code.

```yaml
# config/tools/git.yaml
name: git                     # tool name prefix; default: file name
binary: git                   # looked up on PATH, or an absolute path
env: [GIT_CONFIG_GLOBAL]      # extra environment variables to pass (default: minimal set only)
tools:
  - name: log                 # MCP tool name becomes git_log
    description: Show recent commits
    risk: low                 # low | medium | high | critical (default: high)
    timeout_s: 60             # default 120, max 600
    cwd: "{repo}"             # optional working directory template
    args:
      repo:  { type: path, must_exist: true, description: Repository folder }
      count: { type: integer, default: 20, min: 1, max: 500 }
      path:  { type: path, required: false }
    argv: ["log", "--oneline", "-n", "{count}", {when: path, then: ["--", "{path}"]}]
    writes: []                # path arguments this tool changes; the Gateway snapshots them
```

Safety:
- The program runs from an argv list, never through a shell, so values cannot inject commands.
- String values may not start with "-" (no smuggled options) unless the argument sets `allow_dash: true`.
- Path values are made absolute, so they never look like options either.
- `.bat` and `.cmd` programs are refused: Windows runs them through cmd.exe, which re-parses arguments.

Argument types: string (enum, pattern, max_length, allow_dash), path (must_exist), integer and
number (min, max), boolean. An argument is required unless it has a `default` or `required: false`.
`{when: arg, then: [...], else: [...]}` adds items only if the argument is set and not false.
An optional argument without a default may only appear inside a `when` block for itself.
"""

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

RISKS = ("low", "medium", "high", "critical")
TYPES = {"string": "string", "path": "string", "integer": "integer", "number": "number", "boolean": "boolean"}
ARG_KEYS = {"type", "description", "default", "required", "enum", "pattern", "max_length", "allow_dash",
            "must_exist", "min", "max"}
TOOL_KEYS = {"name", "description", "risk", "timeout_s", "cwd", "args", "argv", "writes"}
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
DEFAULT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 600


class ManifestError(ValueError):
    pass


class ArgumentError(ValueError):
    """A tool call's arguments are invalid. The message goes back to the LLM."""


@dataclass
class Arg:
    name: str
    type: str
    description: str = ""
    default: Any = None
    required: bool = True
    enum: list[Any] | None = None
    pattern: str | None = None
    max_length: int | None = None
    allow_dash: bool = False
    must_exist: bool = False
    min: float | None = None
    max: float | None = None

    def schema(self) -> dict[str, Any]:
        s: dict[str, Any] = {"type": TYPES[self.type]}
        if self.description:
            s["description"] = self.description
        if self.type == "path":
            s["description"] = (self.description + " " if self.description else "") + "(file system path)"
        if self.default is not None:
            s["default"] = self.default
        if self.enum is not None:
            s["enum"] = self.enum
        if self.pattern:
            s["pattern"] = self.pattern
        if self.max_length:
            s["maxLength"] = self.max_length
        if self.min is not None:
            s["minimum"] = self.min
        if self.max is not None:
            s["maximum"] = self.max
        return s

    def check(self, value: Any) -> Any:
        """Validate one value and return it normalized (paths made absolute)."""
        t = self.type
        if t == "boolean":
            if not isinstance(value, bool):
                raise ArgumentError(f"{self.name} must be true or false")
            return value
        if t in ("integer", "number"):
            if isinstance(value, bool) or not isinstance(value, int if t == "integer" else (int, float)):
                raise ArgumentError(f"{self.name} must be an {t}")
            if (self.min is not None and value < self.min) or (self.max is not None and value > self.max):
                raise ArgumentError(f"{self.name} must be between {self.min} and {self.max}")
            return value
        if not isinstance(value, str):
            raise ArgumentError(f"{self.name} must be a string")
        if "\0" in value:
            raise ArgumentError(f"{self.name} contains a NUL character")
        if self.max_length is not None and len(value) > self.max_length:
            raise ArgumentError(f"{self.name} is longer than {self.max_length} characters")
        if self.enum is not None and value not in self.enum:
            raise ArgumentError(f"{self.name} must be one of {self.enum}")
        if t == "path":
            p = Path(value).expanduser().resolve()
            if self.must_exist and not p.exists():
                raise ArgumentError(f"{self.name}: path does not exist: {p}")
            return str(p)
        if self.pattern and not re.fullmatch(self.pattern, value):
            raise ArgumentError(f"{self.name} must match {self.pattern}")
        if value.startswith("-") and not self.allow_dash:
            raise ArgumentError(f"{self.name} may not start with '-'")
        return value


@dataclass
class CliTool:
    name: str  # full MCP name: <manifest>_<tool>
    description: str
    binary: Path
    args: dict[str, Arg]
    argv: list[Any]
    risk: str = "high"
    timeout_s: float = DEFAULT_TIMEOUT_S
    cwd: str | None = None
    writes: list[str] = field(default_factory=list)
    env: list[str] = field(default_factory=list)

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {n: a.schema() for n, a in self.args.items()},
            "required": [n for n, a in self.args.items() if a.required and a.default is None],
            "additionalProperties": False,
        }

    def bind(self, values: dict[str, Any]) -> dict[str, Any]:
        """Check call arguments against the declared ones and fill defaults."""
        unknown = set(values) - set(self.args)
        if unknown:
            raise ArgumentError(f"Unknown arguments: {sorted(unknown)}")
        bound: dict[str, Any] = {}
        for name, arg in self.args.items():
            value = values.get(name, arg.default)
            if value is None:
                if arg.required and arg.default is None:
                    raise ArgumentError(f"Missing argument: {name}")
                bound[name] = None
            else:
                bound[name] = arg.check(value)
        return bound

    def render(self, bound: dict[str, Any]) -> list[str]:
        return [str(self.binary), *_render(self.argv, bound)]

    def render_cwd(self, bound: dict[str, Any]) -> Path | None:
        if self.cwd is None:
            return None
        return Path(_substitute(self.cwd, bound)).expanduser().resolve()


def _substitute(template: str, bound: dict[str, Any]) -> str:
    def value(m: re.Match[str]) -> str:
        v = bound[m.group(1)]
        if v is None:
            raise ArgumentError(f"Argument {m.group(1)} is required here")
        return str(v).lower() if isinstance(v, bool) else str(v)

    return PLACEHOLDER.sub(value, template)


def _render(items: list[Any], bound: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for item in items:
        if isinstance(item, dict):
            v = bound.get(item["when"])
            if v is not None and v is not False and v != "":
                out.extend(_render(item["then"], bound))
            else:
                out.extend(_render(item.get("else", []), bound))
        else:
            out.append(_substitute(str(item), bound))
    return out


# Loading


def resolve_binary(binary: str) -> Path | None:
    p = Path(binary).expanduser()
    found = str(p) if p.is_absolute() and p.is_file() else shutil.which(binary)
    return Path(found) if found else None


def load_manifest(path: Path) -> list[CliTool]:
    """Tools of one manifest. Raises ManifestError for a bad file; returns [] if the binary is missing."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    where = path.name
    prefix = data.get("name", path.stem)
    if not NAME.match(prefix):
        raise ManifestError(f"{where}: bad name {prefix!r}")
    if "binary" not in data:
        raise ManifestError(f"{where}: needs 'binary'")
    binary = resolve_binary(data["binary"])
    if binary is None:
        return []
    if binary.suffix.lower() in (".bat", ".cmd"):
        raise ManifestError(f"{where}: {binary.name} is a batch file; cmd.exe re-parses its arguments, which "
                            "allows injection. Point 'binary' at the real executable instead.")
    tools = []
    for i, spec in enumerate(data.get("tools") or []):
        tools.append(_tool(f"{where} tools[{i}]", prefix, binary, spec, list(data.get("env") or [])))
    names = [t.name for t in tools]
    if len(names) != len(set(names)):
        raise ManifestError(f"{where}: duplicate tool names")
    return tools


def _tool(where: str, prefix: str, binary: Path, spec: dict[str, Any], env: list[str]) -> CliTool:
    unknown = set(spec) - TOOL_KEYS
    if unknown:
        raise ManifestError(f"{where}: unknown keys {sorted(unknown)}")
    name = spec.get("name", "")
    if not NAME.match(name):
        raise ManifestError(f"{where}: bad tool name {name!r}")
    risk = spec.get("risk", "high")
    if risk not in RISKS:
        raise ManifestError(f"{where}: risk must be one of {RISKS}")
    args = {}
    for arg_name, a in (spec.get("args") or {}).items():
        bad = set(a) - ARG_KEYS
        if bad:
            raise ManifestError(f"{where}: argument {arg_name}: unknown keys {sorted(bad)}")
        if a.get("type") not in TYPES:
            raise ManifestError(f"{where}: argument {arg_name}: type must be one of {list(TYPES)}")
        args[arg_name] = Arg(name=arg_name, **{k: v for k, v in a.items()})
        if "required" not in a:
            args[arg_name].required = a.get("default") is None
    argv = spec.get("argv")
    if not isinstance(argv, list):
        raise ManifestError(f"{where}: needs an 'argv' list")
    _check_argv(where, argv, args, guarded=set())
    if spec.get("cwd"):
        _check_template(where, spec["cwd"], args, guarded=set())
    writes = list(spec.get("writes") or [])
    for w in writes:
        if w not in args or args[w].type != "path":
            raise ManifestError(f"{where}: writes entry {w!r} must name a path argument")
    timeout = min(float(spec.get("timeout_s", DEFAULT_TIMEOUT_S)), MAX_TIMEOUT_S)
    return CliTool(f"{prefix}_{name}", spec.get("description", ""), binary, args, argv, risk, timeout,
                   spec.get("cwd"), writes, env)


def _check_argv(where: str, items: list[Any], args: dict[str, Arg], guarded: set[str]) -> None:
    for item in items:
        if isinstance(item, dict):
            if not ({"when", "then"} <= set(item) <= {"when", "then", "else"}) or item["when"] not in args                     or not isinstance(item["then"], list) or not isinstance(item.get("else", []), list):
                raise ManifestError(f"{where}: a block needs 'when: <argument>', a 'then' list, "
                                    f"and optionally an 'else' list: {item}")
            _check_argv(where, item["then"], args, guarded | {item["when"]})
            _check_argv(where, item.get("else", []), args, guarded)
        else:
            _check_template(where, str(item), args, guarded)


def _check_template(where: str, template: str, args: dict[str, Arg], guarded: set[str]) -> None:
    for name in PLACEHOLDER.findall(template):
        if name not in args:
            raise ManifestError(f"{where}: placeholder {{{name}}} is not a declared argument")
        a = args[name]
        if not a.required and a.default is None and name not in guarded:
            raise ManifestError(f"{where}: optional argument {name} must be inside a 'when: {name}' block")


def load_manifests(folder: Path) -> tuple[list[CliTool], list[str]]:
    """All tools in a folder, plus notes about skipped manifests (missing binaries)."""
    tools, notes = [], []
    for path in sorted(folder.glob("*.yaml")):
        loaded = load_manifest(path)
        if not loaded:
            notes.append(f"{path.name}: binary not found, skipped")
        tools.extend(loaded)
    names = [t.name for t in tools]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ManifestError(f"Duplicate tool names across manifests: {sorted(dupes)}")
    return tools, notes
