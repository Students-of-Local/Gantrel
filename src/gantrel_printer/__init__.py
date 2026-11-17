"""Gantrel printer and camera CLI.

One adapter contract for every printer type (docs/PRINTERS.md). No code path here can start or
resume a print without a confirm token that a human minted at a terminal (see safety.py).
"""

__version__ = "0.1.0"
