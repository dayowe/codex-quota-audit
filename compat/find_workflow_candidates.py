#!/usr/bin/env python3
"""Deprecated compatibility wrapper for the workflow candidate finder."""
from _cqa_compat import reexport

_impl = reexport(globals(), "cqa.workflow.candidates")

if __name__ == "__main__":
    raise SystemExit(_impl.main())
