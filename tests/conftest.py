"""Import the plugin directory as package ``cup_plugin`` (mirrors the Hermes loader). Run with a hermes-agent
checkout importable, e.g. ``hermes-agent/scripts/run-in-hermes-env python -m pytest <plugin>/tests``."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PACKAGE = "cup_plugin"
_ROOT = Path(__file__).resolve().parents[1]
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _ROOT]

if PACKAGE not in sys.modules:
    spec = importlib.util.spec_from_file_location(PACKAGE, _ROOT / "__init__.py",
                                                  submodule_search_locations=[str(_ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)


class _RootIsNotATestPackage:
    """The plugin root has an ``__init__.py`` with relative imports; collect it as a plain directory."""

    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == _ROOT:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config):
    if not config.pluginmanager.has_plugin("cup-root"):
        config.pluginmanager.register(_RootIsNotATestPackage(), "cup-root")
