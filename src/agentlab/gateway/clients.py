"""Upstream client identities for HTTP mode.

Each bearer token maps to one client name and one profile. The token itself never goes in the
file: give either an environment variable that holds it, or its SHA-256 hash.

```yaml
# config/clients.yaml
allowed_origins: ["http://127.0.0.1:5173", "http://localhost:5173"]   # browsers allowed to call /mcp
clients:
  cursor:   { token_env: AL_TOKEN_CURSOR, profile: coding }
  my-agent: { token_sha256: "9f86d0...", profile: full-trust }
```

    python -m agentlab.gateway.clients new-token     # prints a token and its hash
"""

import argparse
import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

DEFAULT_ORIGINS = ["http://127.0.0.1:5173", "http://localhost:5173"]


@dataclass(frozen=True)
class ClientIdentity:
    name: str
    profile: str
    token_sha256: str


@dataclass
class Clients:
    clients: list[ClientIdentity]
    allowed_origins: list[str]

    def identify(self, authorization: str | None) -> ClientIdentity | None:
        """Client for an `Authorization: Bearer <token>` header, or None."""
        if not authorization or not authorization.lower().startswith("bearer "):
            return None
        digest = sha256(authorization[7:].strip())
        found = None
        for c in self.clients:  # check every entry, so timing does not reveal which one matched
            if hmac.compare_digest(c.token_sha256, digest):
                found = c
        return found


def sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def load_clients(path: Path) -> Clients:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = []
    for name, spec in (data.get("clients") or {}).items():
        if "profile" not in spec:
            raise ValueError(f"{path}: client {name!r} needs a 'profile'")
        if "token_sha256" in spec:
            digest = str(spec["token_sha256"]).lower()
        elif "token_env" in spec:
            token = os.environ.get(spec["token_env"])
            if not token:
                log.warning("Client %s disabled: environment variable %s is not set", name, spec["token_env"])
                continue
            digest = sha256(token)
        else:
            raise ValueError(f"{path}: client {name!r} needs 'token_env' or 'token_sha256'")
        out.append(ClientIdentity(name, spec["profile"], digest))
    digests = [c.token_sha256 for c in out]
    if len(digests) != len(set(digests)):
        raise ValueError(f"{path}: two clients share a token")
    return Clients(out, list(data.get("allowed_origins") or DEFAULT_ORIGINS))


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m agentlab.gateway.clients")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("new-token", help="generate a random token and print its hash for clients.yaml")
    parser.parse_args()
    token = secrets.token_urlsafe(32)
    print(f"token:        {token}")
    print(f"token_sha256: {sha256(token)}")
    print("Give the token to the client (Authorization: Bearer <token>). Put only the hash in clients.yaml.")


if __name__ == "__main__":
    main()
