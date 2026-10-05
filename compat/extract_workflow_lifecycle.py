#!/usr/bin/env python3
"""Deprecated compatibility wrapper for the workflow lifecycle extractor."""
from _cqa_compat import reexport

_impl = reexport(globals(), "cqa.workflow.lifecycle")

if __name__ == "__main__":
    raise SystemExit(_impl.main())
