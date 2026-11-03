"""Tool add-ons: each tool is one Python file.

A tool file defines:

    NAME = "search"                       # what Claude calls it
    DESCRIPTION = "..."                   # what Claude reads to decide when to use it
    def handler(ctx, query: str, limit: int = 8) -> dict | list | str: ...

The handler's parameters after `ctx` (with type hints, optionally
Annotated[..., Field(description=...)]) become the tool's input schema.
`ctx` is a ToolContext: the only way a tool reaches documents, and every
method on it is already limited to the active collections. No tool takes a
collection argument, so a tool cannot widen the scope.

Built-in tools live in the collection_search.tools package. User add-ons are
.py files in a `tools` folder next to the program or in the settings folder.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import os
import pkgutil
import typing
from dataclasses import asdict, dataclass
from types import ModuleType
from typing import Any, Callable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    handler: Callable[..., Any]
    source: str  # "built-in" or the add-on file path


class ToolContext:
    """What a tool may do. Every call is scoped to the active collections."""

    def __init__(self, engine):
        self._engine = engine

    def search(self, query: str, limit: int | None = None) -> list[dict]:
        if limit is None:
            limit = self._engine.settings.preferences.default_result_count
        return [asdict(r) | {"collections": list(r.collections)} for r in self._engine.search(query, limit)]

    def read_file(self, path: str) -> dict:
        f = self._engine.read_file(path)
        return {"path": f.path, "collections": list(f.collections), "text": f.text, "truncated": f.truncated}

    def active_collections(self) -> list[str]:
        return self._engine.active_collection_names()


def load_tools(extra_dirs: list[str] | None = None) -> list[ToolSpec]:
    """Built-in tools first, then add-on files. A broken add-on is logged and skipped."""
    specs: dict[str, ToolSpec] = {}
    from . import tools as builtin

    for info in sorted(pkgutil.iter_modules(builtin.__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{builtin.__name__}.{info.name}")
        _add(specs, module, "built-in")
    for folder in extra_dirs or []:
        if not os.path.isdir(folder):
            continue
        for filename in sorted(os.listdir(folder)):
            if not filename.endswith(".py") or filename.startswith("_"):
                continue
            path = os.path.join(folder, filename)
            try:
                module = _load_file(path)
                _add(specs, module, path)
            except Exception:
                log.exception("Skipping add-on %s", path)
    return list(specs.values())


def _load_file(path: str) -> ModuleType:
    name = "collection_search_addon_" + os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _add(specs: dict[str, ToolSpec], module: ModuleType, source: str) -> None:
    name = getattr(module, "NAME", None)
    description = getattr(module, "DESCRIPTION", None)
    handler = getattr(module, "handler", None)
    if not (isinstance(name, str) and isinstance(description, str) and callable(handler)):
        raise ValueError(f"{source}: a tool needs NAME, DESCRIPTION and handler(ctx, ...)")
    if name in specs:
        log.warning("Tool %r from %s ignored: %s already defines it", name, source, specs[name].source)
        return
    specs[name] = ToolSpec(name, description.strip(), handler, source)


def bind(spec: ToolSpec, ctx: ToolContext, before_call: Callable[[str], None]) -> Callable[..., Any]:
    """A plain function for the MCP server: same inputs minus ctx, with the gate check first."""
    hints = typing.get_type_hints(spec.handler, include_extras=True)
    params = list(inspect.signature(spec.handler).parameters.values())[1:]
    params = [p.replace(annotation=hints.get(p.name, p.annotation)) for p in params]

    def tool(**kwargs):
        before_call(spec.name)
        return spec.handler(ctx, **kwargs)

    tool.__name__ = spec.name
    tool.__doc__ = spec.description
    tool.__signature__ = inspect.Signature(params, return_annotation=hints.get("return", inspect.Signature.empty))
    tool.__annotations__ = {p.name: p.annotation for p in params}
    if "return" in hints:
        tool.__annotations__["return"] = hints["return"]
    return tool
