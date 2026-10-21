from importlib.metadata import PackageNotFoundError, version

try:
    # The distribution name: it must match `name` in pyproject.toml
    __version__ = version("cypher-guardrail")
except PackageNotFoundError:
    __version__ = "unknown"

from cypher_guardrail._internal.core.errors import CypherError, GraphNotFoundError
from cypher_guardrail.live import AsyncLiveSnapshot, LiveSnapshot, StaleSnapshotError
from cypher_guardrail.snapshot import Snapshot, SnapshotSource
from cypher_guardrail.validators.advisor import Advisor, AsyncAdvisor
from cypher_guardrail.validators.properties_validator import (
    AsyncPropertiesValidator,
    PropertiesValidator,
)
from cypher_guardrail.validators.schema_validator import (
    AsyncSchemaValidator,
    SchemaValidator,
)
from cypher_guardrail.validators.syntax_validator import (
    AsyncSyntaxValidator,
    SyntaxValidator,
)

__all__ = [
    "Advisor",
    "AsyncAdvisor",
    "AsyncLiveSnapshot",
    "AsyncPropertiesValidator",
    "AsyncSchemaValidator",
    "AsyncSyntaxValidator",
    "CypherError",
    "GraphNotFoundError",
    "LiveSnapshot",
    "PropertiesValidator",
    "SchemaValidator",
    "Snapshot",
    "SnapshotSource",
    "StaleSnapshotError",
    "SyntaxValidator",
    "__version__",
]
