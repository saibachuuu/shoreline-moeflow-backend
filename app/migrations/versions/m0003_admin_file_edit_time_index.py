"""Index the administrator's global file list by edit time."""

VERSION = "0003"
NAME = "admin_file_edit_time_index"
DESCRIPTION = "Add the descending edit-time index used by the administrator file list"
LEVEL = "AUTO"

INDEX_NAME = "file_admin_edit_time_v1"
INDEX_KEYS = [("et", -1)]


def up(db):
    db.file.create_index(INDEX_KEYS, name=INDEX_NAME, background=True)


def down(db):
    db.file.drop_index(INDEX_NAME)


def verify(db):
    return INDEX_NAME in db.file.index_information()
