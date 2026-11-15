"""Prompt files: Markdown or YAML with a front-matter header carrying id and version.

    ---
    id: excuse-compiler
    version: 3
    ---
    body...

The version is logged with every LLM call so eval results can be tied to a prompt revision.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from importlib import resources
from string import Template
from typing import Any

import yaml


@dataclass(frozen=True)
class Prompt:
    id: str
    version: int
    body: str

    def render(self, **values: Any) -> str:
        """Substitute `$name` placeholders; missing values raise `KeyError`."""
        return Template(self.body).substitute(**{k: str(v) for k, v in values.items()})


def parse_prompt(text: str) -> Prompt:
    """Split a prompt file into header and body."""
    if not text.startswith("---"):
        raise ValueError("prompt file must start with a '---' front-matter header")
    _, header, body = text.split("---", 2)
    meta = yaml.safe_load(header) or {}
    if "id" not in meta or "version" not in meta:
        raise ValueError("prompt header needs 'id' and 'version'")
    return Prompt(id=str(meta["id"]), version=int(meta["version"]), body=body.lstrip("\n"))


@cache
def load_prompt(package: str, name: str) -> Prompt:
    """Load `name` from the `prompts` directory of `package`, e.g. ('imbusy.excuse', 'compiler.md')."""
    text = resources.files(package).joinpath("prompts", name).read_text(encoding="utf-8")
    return parse_prompt(text)


@cache
def load_yaml_prompt(package: str, name: str) -> tuple[Prompt, dict[str, Any]]:
    """Load a YAML prompt file; returns the header and the parsed body."""
    prompt = load_prompt(package, name)
    return prompt, yaml.safe_load(prompt.body)
