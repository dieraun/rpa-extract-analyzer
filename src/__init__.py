"""rpa-loader-analyzer.

A local, read-only research toolkit for Ren'Py ``loader.pyo`` files and
modified RPA archives.  The package never executes decompiled game code and
never deserialises pickle payloads with :mod:`pickle`.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.3.0"
