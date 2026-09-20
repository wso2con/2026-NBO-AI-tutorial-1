"""Load harness task contracts colocated with model-readable Skills."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_skill_contract(skills_root: Path, skill_name: str) -> dict[str, Any] | None:
    """Return ``<skill>/contract.yaml`` when the selected Skill defines one.

    The directory name is validated so a model-provided skill name cannot
    escape the configured Skills directory.
    """
    if not skill_name or Path(skill_name).name != skill_name:
        return None
    path = (skills_root / skill_name / "contract.yaml").resolve()
    root = skills_root.resolve()
    if root not in path.parents or not path.is_file():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return raw if isinstance(raw, dict) else None
