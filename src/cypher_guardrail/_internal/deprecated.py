"""Names that a module used to export and no longer does: the old import still works, with a ``DeprecationWarning``."""

import importlib
import warnings
from collections.abc import Callable


def moved(module: str, names: dict[str, str | None]) -> Callable[[str], object]:
    """The ``__getattr__`` of ``module``, for the old ``names``: ``{name: where to import it from now}``.

    A name that has no new home (``None``) is only going away. All of them are removed in 2.0.
    """

    def __getattr__(name: str) -> object:
        if name not in names:
            raise AttributeError(f"module {module!r} has no attribute {name!r}")
        home = names[name]
        advice = f"import it from {home}" if home else "it is not public any more"
        warnings.warn(
            f"{module}.{name} is deprecated ({advice}) and will be removed in 2.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        return getattr(importlib.import_module(home or _OLD_HOMES[module]), name)

    return __getattr__


# where a name that has no new home still lives
_OLD_HOMES = {"cypher_guardrail.utils": "cypher_guardrail._internal.sources.online"}
