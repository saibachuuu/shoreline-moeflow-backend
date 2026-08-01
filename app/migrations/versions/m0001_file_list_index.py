"""Indexes for the hot project-file list path."""

VERSION = "0001"
NAME = "file_list_index"
DESCRIPTION = "Add the compound index used by the default file-list query"
LEVEL = "AUTO"

INDEX_NAME = "file_list_v2"
INDEX_KEYS = [("p", 1), ("ac", 1), ("f", 1), ("dn", 1), ("t", 1), ("sn", 1)]


def up(db):
    db.file.create_index(INDEX_KEYS, name=INDEX_NAME, background=True)


def down(db):
    db.file.drop_index(INDEX_NAME)


def verify(db):
    return INDEX_NAME in db.file.index_information()
