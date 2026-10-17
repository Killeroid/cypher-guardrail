from importlib.metadata import PackageNotFoundError, version

try:
    # The distribution name: it must match `name` in pyproject.toml
    __version__ = version("cypher-guardrail")
except PackageNotFoundError:
    __version__ = "unknown"

from cypher_guardrail.snapshot import Snapshot, SnapshotSource
from cypher_guardrail.validators.syntax_validator import (
    AsyncSyntaxValidator,
    SyntaxValidator,
)

__all__ = [
    "AsyncSyntaxValidator",
    "Snapshot",
    "SnapshotSource",
    "SyntaxValidator",
    "__version__",
]
