#!/usr/bin/env python3
"""
Extractor auto-discovery registry.

Imports all modules in this package, instantiates each Extractor subclass,
and returns those whose detect() method matches the given repo.

Adding a new stack = drop a new .py file here with a class that inherits
from Extractor. No registration boilerplate needed.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from .base import Extractor


def _find_extractor_classes() -> list[type[Extractor]]:
    """Import all sibling modules and collect Extractor subclasses."""
    classes: list[type[Extractor]] = []
    package_dir = Path(__file__).parent

    for info in pkgutil.iter_modules([str(package_dir)]):
        if info.name == "base":
            continue
        module = importlib.import_module(f".{info.name}", package=__package__)
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, Extractor)
                and attr is not Extractor
            ):
                classes.append(attr)

    return classes


def get_extractors(repo_path: Path) -> list[Extractor]:
    """Return instantiated extractors that detect their stack in the repo."""
    result: list[Extractor] = []
    for cls in _find_extractor_classes():
        instance = cls()
        try:
            if instance.detect(repo_path):
                result.append(instance)
        except Exception:
            # If detection fails, skip this extractor silently.
            pass
    return result


__all__ = ["get_extractors", "Extractor"]
