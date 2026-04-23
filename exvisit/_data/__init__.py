from __future__ import annotations

from importlib.resources import files


def read_text(name: str) -> str:
    return files(__name__).joinpath(name).read_text(encoding="utf-8")