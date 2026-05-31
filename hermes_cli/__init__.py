# hermes_cli/__init__.py — back-compat package shim (Hermes->Thoth rename, P4). Remove in cleanup phase.
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys

import thoth_cli

_OLD = "hermes_cli"
_NEW = "thoth_cli"


class _ThothAliasLoader(importlib.abc.Loader):
    def __init__(self, new_name: str) -> None:
        self._new_name = new_name

    def create_module(self, spec):
        return importlib.import_module(self._new_name)

    def exec_module(self, module):
        sys.modules[module.__name__] = module

    def get_code(self, fullname):
        # find_spec (locate-only), NOT import_module — avoids double-executing main.py under -m.
        real_spec = importlib.util.find_spec(self._new_name)
        return real_spec.loader.get_code(self._new_name)

    def get_filename(self, fullname):
        # So runpy sets a real __file__ under `python -m hermes_cli.main`.
        real_spec = importlib.util.find_spec(self._new_name)
        return real_spec.origin


class _ThothAliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != _OLD and not fullname.startswith(_OLD + "."):
            return None
        new_name = _NEW + fullname[len(_OLD):]
        # Carry the REAL module's origin + package search locations onto the alias
        # spec. Without origin, `python -m hermes_cli.main` runs with __file__=None
        # (runpy sets __file__ from spec.origin), and thoth_cli/main.py dereferences
        # Path(__file__) at import scope -> TypeError. Verified on CPython 3.11+3.12.
        real_spec = importlib.util.find_spec(new_name)
        spec = importlib.machinery.ModuleSpec(
            fullname,
            _ThothAliasLoader(new_name),
            origin=(real_spec.origin if real_spec else None),
            is_package=bool(real_spec and real_spec.submodule_search_locations),
        )
        if real_spec and real_spec.submodule_search_locations:
            spec.submodule_search_locations = list(real_spec.submodule_search_locations)
        return spec


sys.modules[_OLD] = thoth_cli
if not any(isinstance(f, _ThothAliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _ThothAliasFinder())
