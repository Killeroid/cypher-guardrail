from importlib.metadata import PackageNotFoundError, version

try:
    # The distribution name: it must match `name` in pyproject.toml
    __version__ = version("cypher-guardrail")
except PackageNotFoundError:
    __version__ = "unknown"

__all__ = ["__version__"]
