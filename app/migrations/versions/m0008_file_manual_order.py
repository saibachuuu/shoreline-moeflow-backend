"""Index only: old files deliberately retain a null/missing manual order."""

VERSION = "0008"
NAME = "file_manual_order"
DESCRIPTION = "Add directory-local manual file ordering index"
LEVEL = "AUTO"
INDEX_NAME = "file_manual_order_v1"
INDEX_KEYS = [
    ("p", 1),
    ("ac", 1),
    ("f", 1),
    ("dn", 1),
    ("t", 1),
    ("mo", 1),
    ("sn", 1),
    ("_id", 1),
]


def up(db):
    db.file.create_index(INDEX_KEYS, name=INDEX_NAME, background=True)


def down(db):
    db.file.drop_index(INDEX_NAME)


def verify(db):
    return INDEX_NAME in db.file.index_information()
