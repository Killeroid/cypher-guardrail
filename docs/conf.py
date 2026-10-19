"""Sphinx configuration for the cypher-guardrail documentation."""

from importlib.metadata import version as _version

project = "cypher-guardrail"
author = "Killeroid"
copyright = "2026, Killeroid"
release = _version("cypher-guardrail")
version = release

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "sphinx_copybutton",
]

source_suffix = {".md": "markdown", ".rst": "restructuredtext"}
exclude_patterns = ["_build", "TODO.md"]

# The guides are Markdown; link to `#anchors` of headings down to ### from other pages
myst_heading_anchors = 3

autodoc_member_order = "bysource"
autodoc_typehints = "description"
napoleon_google_docstring = True
napoleon_numpy_docstring = False

intersphinx_mapping = {"python": ("https://docs.python.org/3", None)}

html_theme = "furo"
html_title = f"cypher-guardrail {release}"
