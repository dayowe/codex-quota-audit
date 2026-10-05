#!/usr/bin/env python3
"""Deprecated compatibility wrapper for the workflow pause helpers."""
from _cqa_compat import reexport

_impl = reexport(globals(), "cqa.workflow.pauses")
