#!/usr/bin/env python3
"""Deprecated compatibility wrapper for the unified CLI."""
from _cqa_compat import reexport

_impl = reexport(globals(), "cqa.cli")

if __name__ == "__main__":
    raise SystemExit(_impl.main())
