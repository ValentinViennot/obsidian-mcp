"""Validated connection names and stable, non-deliverable Git author identities."""
from dataclasses import dataclass
import hashlib
import re
import unicodedata


def normalize_connection_alias(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("A connection name is required.")
    value = unicodedata.normalize("NFC", value).strip()
    if not value or len(value) > 128 or any(
        unicodedata.category(char) in {"Cc", "Cf"} or char in "<>" for char in value
    ):
        raise ValueError("Use a connection name of 1–128 characters without control characters or angle brackets.")
    return value


@dataclass(frozen=True)
class ConnectionAuthor:
    name: str
    email: str

    def __post_init__(self):
        if normalize_connection_alias(self.name) != self.name or not re.fullmatch(
            r"[0-9a-f]{32}@connections\.obsidian-mcp\.invalid", self.email
        ):
            raise ValueError("Invalid connection author snapshot.")


def connection_author(alias: str, kind: str, identifier: str | int) -> ConnectionAuthor:
    name = normalize_connection_alias(alias)
    if kind not in {"oauth", "api_key"} or identifier is None or str(identifier) == "":
        raise ValueError("A stable credential identity is required.")
    digest = hashlib.sha256(f"{kind}\0{identifier}".encode("utf-8")).hexdigest()[:32]
    return ConnectionAuthor(name, f"{digest}@connections.obsidian-mcp.invalid")
