"""Pytest bootstrap for the backend test suite.

Root conftest runs before any test module imports the app, so compiled
gettext catalogs are ready by then.  ``*.mo`` is gitignored and a fresh
checkout (e.g. CI) has none; without them gettext falls back to the
Chinese msgids and locale-dependent assertions (the English proofread
draft email) fail.  Compile them here so the suite is self-contained.
"""

import os
import tempfile
from pathlib import Path

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po


def _compile_translation_catalogs() -> None:
    base_dir = Path(__file__).parent / "app" / "translations"
    for po_path in base_dir.glob("*/LC_MESSAGES/*.po"):
        with open(po_path, "rb") as po_file:
            catalog = read_po(po_file)
        # Write via a temp file and atomic replace so parallel pytest
        # workers never observe a half-written catalog.
        fd, tmp_path = tempfile.mkstemp(dir=po_path.parent, suffix=".mo")
        try:
            with os.fdopen(fd, "wb") as mo_file:
                write_mo(mo_file, catalog)
            os.replace(tmp_path, po_path.with_suffix(".mo"))
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)


_compile_translation_catalogs()
