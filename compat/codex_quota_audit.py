#!/usr/bin/env python3
"""Deprecated compatibility wrapper for the quota audit."""
from _cqa_compat import reexport

_impl = reexport(globals(), "cqa.quota.audit")

if __name__ == "__main__":
    raise SystemExit(_impl.main())
