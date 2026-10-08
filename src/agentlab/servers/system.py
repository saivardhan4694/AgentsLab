"""System information MCP server. All tools are read-only.

Run standalone:  uv run agentlab-system
"""

import os
import platform
import shutil
import sys
from typing import Any

import psutil
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

# Environment variable values can hold secrets. Only these are returned by value.
SAFE_ENV_VARS = {"PATH", "PATHEXT", "OS", "HOME", "USERPROFILE", "USERNAME", "USER", "SHELL", "COMSPEC",
                 "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "TEMP", "TMP", "LANG", "TERM"}


def _gb(n: int) -> float:
    return round(n / 1024**3, 2)


def _installed_apps_windows() -> list[dict[str, str]]:
    import winreg

    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    apps: dict[str, dict[str, str]] = {}
    for hive, path in keys:
        try:
            root = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    with winreg.OpenKey(root, winreg.EnumKey(root, i)) as sub:
                        name = winreg.QueryValueEx(sub, "DisplayName")[0]
                        app = {"name": name}
                        for field, value_name in [("version", "DisplayVersion"), ("publisher", "Publisher"),
                                                  ("install_location", "InstallLocation")]:
                            try:
                                if value := winreg.QueryValueEx(sub, value_name)[0]:
                                    app[field] = str(value)
                            except OSError:
                                pass
                        apps.setdefault(name, app)
                except OSError:
                    continue
    return sorted(apps.values(), key=lambda a: a["name"].lower())


def create_server() -> MCPServer:
    server = MCPServer("agentlab-system", version="0.1.0",
                       instructions="Read-only information about the local machine.")

    @server.tool(annotations=READ_ONLY)
    def os_info() -> dict[str, Any]:
        """Get the operating system, CPU, memory, user, and Python version of this machine."""
        return {
            "os": platform.system(),
            "os_release": platform.release(),
            "os_version": platform.version(),
            "machine": platform.machine(),
            "hostname": platform.node(),
            "cpu": platform.processor(),
            "cpu_cores_logical": psutil.cpu_count(),
            "cpu_cores_physical": psutil.cpu_count(logical=False),
            "memory_total_gb": _gb(psutil.virtual_memory().total),
            "user": os.environ.get("USERNAME") or os.environ.get("USER"),
            "home": os.path.expanduser("~"),
            "python": sys.version.split()[0],
        }

    @server.tool(annotations=READ_ONLY)
    def resource_usage() -> dict[str, Any]:
        """Get current CPU and memory usage."""
        mem = psutil.virtual_memory()
        return {
            "cpu_percent": psutil.cpu_percent(interval=0.5),
            "memory_used_gb": _gb(mem.used),
            "memory_available_gb": _gb(mem.available),
            "memory_percent": mem.percent,
            "boot_time": psutil.boot_time(),
        }

    @server.tool(annotations=READ_ONLY)
    def disk_usage() -> dict[str, Any]:
        """Get total, used, and free space for each mounted disk."""
        disks = []
        for part in psutil.disk_partitions(all=False):
            try:
                u = psutil.disk_usage(part.mountpoint)
            except OSError:
                continue  # empty card reader, unmounted drive
            disks.append({"mount": part.mountpoint, "fs": part.fstype, "total_gb": _gb(u.total),
                          "used_gb": _gb(u.used), "free_gb": _gb(u.free), "percent": u.percent})
        return {"disks": disks}

    @server.tool(annotations=READ_ONLY)
    def installed_apps(name_contains: str = "") -> dict[str, Any]:
        """List installed applications (Windows only). Optionally filter by a case-insensitive name substring."""
        if sys.platform != "win32":
            raise ToolError("installed_apps is only implemented on Windows")
        apps = _installed_apps_windows()
        if name_contains:
            apps = [a for a in apps if name_contains.lower() in a["name"].lower()]
        return {"count": len(apps), "apps": apps}

    @server.tool(annotations=READ_ONLY)
    def env_var_names() -> dict[str, Any]:
        """List environment variable names. Values are returned only for a small set of non-secret variables."""
        names = sorted(os.environ)
        safe = {k: v for k, v in os.environ.items() if k.upper() in SAFE_ENV_VARS}
        return {"names": names, "safe_values": safe}

    @server.tool(annotations=READ_ONLY)
    def which(command: str) -> dict[str, Any]:
        """Find the full path of an installed command-line program, for example 'git' or 'ffmpeg'."""
        path = shutil.which(command)
        return {"command": command, "found": path is not None, "path": path}

    return server


def main() -> None:
    create_server().run()


if __name__ == "__main__":
    main()
