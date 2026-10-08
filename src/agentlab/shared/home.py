import os
from pathlib import Path

HOME_ENV = "AGENTLAB_HOME"


def agentlab_home() -> Path:
    """Folder for local state (snapshots, approvals). `AGENTLAB_HOME` overrides `~/.agentlab`."""
    home = Path(os.environ.get(HOME_ENV, "~/.agentlab")).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    return home
