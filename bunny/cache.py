"""On-disk catalog cache: the UE tool set stays visible to harnesses while UE is closed."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

SCHEMA = 1

_EMPTY = {
    "schema": SCHEMA,
    "captured_at": None,
    "upstream_url": None,
    "upstream_server_info": None,
    "tools": [],
    "resources": [],
    "resource_templates": [],
    "prompts": [],
}


class Cache:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.data = dict(_EMPTY)
        self.load()

    def load(self) -> dict:
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, OSError):
                self.data = dict(_EMPTY)
        return self.data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def update(self, snapshot: dict, upstream_url: str, server_info=None) -> dict:
        self.data = {
            "schema": SCHEMA,
            "captured_at": time.time(),
            "upstream_url": upstream_url,
            "upstream_server_info": server_info,
            "tools": snapshot.get("tools", []),
            "resources": snapshot.get("resources", []),
            "resource_templates": snapshot.get("resourceTemplates", []),
            "prompts": snapshot.get("prompts", []),
        }
        self.save()
        return self.data

    @property
    def tools(self) -> list:
        return self.data.get("tools", [])

    @property
    def captured_at(self):
        return self.data.get("captured_at")

    def tool_names(self) -> set:
        return {t.get("name") for t in self.tools}
