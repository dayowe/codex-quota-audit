#!/usr/bin/env python3
"""Deprecated compatibility wrapper for the report helpers."""
from _cqa_compat import reexport

_impl = reexport(globals(), "cqa.report.core")
