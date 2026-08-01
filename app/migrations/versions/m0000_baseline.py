"""Record the point from which versioned migrations are managed."""

VERSION = "0000"
NAME = "baseline"
DESCRIPTION = "Establish the versioned migration baseline"
LEVEL = "AUTO"


def up(db):
    """The existing schema is the baseline; no data operation is required."""


def down(db):
    """Removing the baseline record is safe because it has no schema changes."""


def verify(db):
    return True
