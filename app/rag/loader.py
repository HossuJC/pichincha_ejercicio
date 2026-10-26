"""Loads the versioned reglamento documents and selects the one in force.

Legal publishes a new `reglamento_v<version>.md` with front-matter (`version`,
`effective_date`, `thresholds`). The active document is the one with the latest
`effective_date` that is not in the future; no code change is needed.
"""

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

_FRONT_MATTER = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_SECTION = re.compile(r"^## +(\d+)\.\s*(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class Thresholds:
    max_days_to_dispute: int
    review_amount_usd: float
    recent_disputes_window_days: int
    max_recent_disputes: int


@dataclass(frozen=True)
class Section:
    section: str
    title: str
    text: str
    version: str


@dataclass(frozen=True)
class Reglamento:
    version: str
    effective_date: date
    thresholds: Thresholds
    sections: list[Section]
    source: Path


def parse_reglamento(path: Path) -> Reglamento:
    raw = path.read_text(encoding="utf-8")
    match = _FRONT_MATTER.match(raw)
    if not match:
        raise ValueError(f"{path.name}: falta el front-matter")
    meta = yaml.safe_load(match.group(1))
    body = raw[match.end():]
    version = str(meta["version"])

    headers = list(_SECTION.finditer(body))
    sections = []
    for i, header in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(body)
        sections.append(
            Section(
                section=header.group(1),
                title=header.group(2).strip(),
                text=body[header.end():end].strip(),
                version=version,
            )
        )
    if not sections:
        raise ValueError(f"{path.name}: no contiene secciones '## N. Título'")

    return Reglamento(
        version=version,
        effective_date=date.fromisoformat(str(meta["effective_date"])),
        thresholds=Thresholds(**meta["thresholds"]),
        sections=sections,
        source=path,
    )


def load_active_reglamento(directory: Path, today: date) -> Reglamento:
    docs = [parse_reglamento(p) for p in sorted(directory.glob("reglamento_*.md"))]
    in_force = [d for d in docs if d.effective_date <= today]
    if not in_force:
        raise RuntimeError(f"No hay reglamento vigente al {today} en {directory}")
    return max(in_force, key=lambda d: d.effective_date)
