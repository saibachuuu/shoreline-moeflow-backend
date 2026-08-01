"""Add persistent status fields for asynchronously generated thumbnails."""

VERSION = "0002"
NAME = "thumbnail_status"
DESCRIPTION = "Add explicit thumbnail lifecycle fields to image files"
LEVEL = "AUTO"

STATUS_FIELD = "th"
ERROR_FIELD = "the"
UNKNOWN = 0


def up(db):
    db.file.update_many(
        {"t": 2, STATUS_FIELD: {"$exists": False}},
        {"$set": {STATUS_FIELD: UNKNOWN, ERROR_FIELD: ""}},
    )


def down(db):
    db.file.update_many({}, {"$unset": {STATUS_FIELD: "", ERROR_FIELD: "", "tht": ""}})


def verify(db):
    return db.file.count_documents({"t": 2, STATUS_FIELD: {"$exists": False}}) == 0
