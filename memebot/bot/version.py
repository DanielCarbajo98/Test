"""Versión del código en ejecución. El actualizador escribe bot/VERSION al aplicar una actualización;
cada proceso largo lo vigila y se reinicia (Docker lo levanta con el código nuevo) cuando cambia."""
from __future__ import annotations

from pathlib import Path

VERSION_FILE = Path(__file__).parent / "VERSION"


def read() -> str:
    try:
        return VERSION_FILE.read_text().strip() or "dev"
    except OSError:
        return "dev"


STARTED_WITH = read()


def changed() -> bool:
    """True si se ha instalado una versión nueva desde que arrancó este proceso."""
    return read() != STARTED_WITH
