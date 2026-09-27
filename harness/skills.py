"""Portable Agent-Skills loader (agentskills.io format): catalog -> SKILL.md body -> references.

Only the catalog (name + description) sits in the static prompt prefix; bodies load on
activation (model-driven via activate_skill, or harness-injected at key phases)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from harness.config import HARNESS_ROOT

SKILLS_DIR = HARNESS_ROOT / "skills"
_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path


def _frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    meta: dict = {}
    for line in text[3:end].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, text[end + 4 :].lstrip("\n")


def load_skills(directory: Path = SKILLS_DIR) -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    if not directory.is_dir():
        return skills
    for p in sorted(directory.glob("*/SKILL.md")):
        meta, body = _frontmatter(p.read_text(encoding="utf-8"))
        name = meta.get("name", "")
        desc = meta.get("description", "")
        if not _NAME_RE.match(name) or name != p.parent.name or not desc or len(name) > 64 or len(desc) > 1024:
            continue  # invalid skills are skipped, never half-loaded
        skills[name] = Skill(name, desc, body.strip(), p.parent)
    return skills


def catalog(skills: dict[str, Skill]) -> str:
    """Skill names only: the catalog is resent on every call and the names are self-describing; the full
    procedure (and its description) is loaded on demand."""
    return ", ".join(skills)


def render(skill: Skill) -> str:
    refs = sorted(x.relative_to(skill.path).as_posix() for x in skill.path.glob("references/*") if x.is_file())
    extra = f"\n(references available on request: {', '.join(refs)})" if refs else ""
    return f'<skill_content name="{skill.name}">\n{skill.body}{extra}\n</skill_content>'
