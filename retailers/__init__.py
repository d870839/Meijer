"""Retailer adapters. Each module exports the constants and functions the
driver needs to set a store and parse search results."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType


SUPPORTED = ("meijer", "sprouts", "foodlion")


def get(name: str) -> ModuleType:
    """Return the retailer module by name. Raises ValueError if unknown."""
    name = name.lower().strip()
    if name not in SUPPORTED:
        raise ValueError(
            f"unknown retailer {name!r}; supported: {', '.join(SUPPORTED)}"
        )
    return import_module(f".{name}", __name__)
