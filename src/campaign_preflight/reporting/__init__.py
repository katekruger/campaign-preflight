"""Report renderers: terminal, JSON, and Markdown.

All three apply redaction at render time and produce deterministic output for
identical input.
"""

from .csv_export import write_affected_csv
from .json_report import load_schema, render_json, report_to_dict
from .markdown import render_markdown
from .redaction import redact_email, redact_samples, redact_text
from .terminal import render_terminal

__all__ = [
    "load_schema",
    "redact_email",
    "redact_samples",
    "redact_text",
    "render_json",
    "render_markdown",
    "render_terminal",
    "report_to_dict",
    "write_affected_csv",
]
