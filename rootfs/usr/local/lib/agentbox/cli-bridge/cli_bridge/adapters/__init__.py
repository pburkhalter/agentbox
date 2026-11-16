"""Adapter registry. Every module here except ``base`` defines ``ADAPTER``;
adding a CLI means adding one module."""

from __future__ import annotations

import importlib
import pkgutil

from .. import errors


def adapter_classes() -> dict:
    classes = {}
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        if info.name == "base" or info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        cls = getattr(module, "ADAPTER", None)
        if cls is not None:
            classes[cls.name] = cls
    return classes


def names() -> tuple:
    return tuple(adapter_classes())


class Registry:
    def __init__(self, cfg, environ=None):
        self.cfg = cfg
        self.adapters = {name: cls(cfg, environ) for name, cls in adapter_classes().items()}

    def enabled(self) -> list:
        return [a for a in self.adapters.values() if a.enabled]

    def resolve(self, model: str):
        """(adapter, CLI model or None for the CLI's default, model id as answered)."""
        name, sep, rest = model.partition("/")
        adapter = self.adapters.get(name)
        if adapter is None:
            known = ", ".join(sorted(self.adapters))
            raise errors.model_not_found(
                f"unknown model {model!r}: use <adapter>/<model> with adapter one of {known} "
                "(GET /v1/models lists what is enabled)"
            )
        if not adapter.enabled:
            raise errors.model_not_found(
                f"the {name} adapter is disabled (AGENTBOX_CLI_BRIDGE_{name.upper()}=1 enables it)"
            )
        if not sep or rest in ("", "default"):
            return adapter, None, name
        models = adapter.models()
        if rest not in models:
            raise errors.model_not_found(
                f"model {rest!r} is not enabled for {name}; accepted: {', '.join(adapter.model_ids()) or name}"
            )
        return adapter, rest, f"{name}/{rest}"
