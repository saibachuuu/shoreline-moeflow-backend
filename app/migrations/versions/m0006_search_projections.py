"""Backfill normalized search fields and create list-query indexes."""

import unicodedata
from pymongo import UpdateOne

from app.migrations.runner import IrreversibleMigration

VERSION = "0006"
NAME = "search_projections"
DESCRIPTION = "Backfill searchable text and add project/member query indexes"
LEVEL = "AUTO"

PROJECT_LIST_INDEX = "project_team_set_status_edit_v1"
PROJECT_LIST_KEYS = (("t", 1), ("ps", 1), ("st", 1), ("et", -1))
PROJECT_TEAM_LIST_INDEX = "project_team_status_edit_v1"
PROJECT_TEAM_LIST_KEYS = (("t", 1), ("st", 1), ("et", -1))
PROJECT_NAME_INDEX = "project_team_set_status_name_search_v1"
PROJECT_NAME_KEYS = (("t", 1), ("ps", 1), ("st", 1), ("ns", 1))
USER_NAME_INDEX = "user_name_search_v1"
USER_NAME_KEYS = (("ns", 1),)
USER_ALIASES_INDEX = "user_aliases_search_v1"
USER_ALIASES_KEYS = (("asrch", 1),)
TEAM_ALIAS_INDEX = "team_member_alias_search_v1"
TEAM_ALIAS_KEYS = (("team", 1), ("status", 1), ("asrch", 1))
MEMBER_NAME_INDEX = "project_member_display_name_search_v1"
MEMBER_NAME_KEYS = (("project", 1), ("status", 1), ("dns", 1))
BACKFILL_BATCH_SIZE = 500


def _search_text(value):
    if not isinstance(value, str):
        return ""
    return unicodedata.normalize("NFC", value).strip().casefold()


def _alias_projection(values):
    return [
        normalized
        for normalized in (_search_text(alias) for alias in (values or []))
        if normalized
    ]


def _create_named_index(collection, name, keys):
    indexes = collection.index_information()
    current = indexes.get(name)
    if current is not None and tuple(current.get("key", [])) == tuple(keys):
        return

    # A prior partial run may have created either the canonical name or an
    # auto-generated name with the same keys.  These non-unique search indexes
    # can safely be replaced by one stable specification.
    names_to_drop = {
        index_name
        for index_name, index in indexes.items()
        if index_name != "_id_"
        and (index_name == name or tuple(index.get("key", [])) == tuple(keys))
    }
    for index_name in names_to_drop:
        collection.drop_index(index_name)
    collection.create_index(keys, name=name, background=True)


def _write_batches(collection, updates):
    # mongomock 4.3 cannot consume PyMongo 4.17 UpdateOne objects because
    # PyMongo now passes the ``sort`` option to its bulk builder. Production
    # MongoDB keeps the faster bulk-write path below.
    if collection.__class__.__module__.startswith("mongomock"):
        for document_id, update in updates:
            collection.update_one({"_id": document_id}, update)
        return

    operations = []
    for document_id, update in updates:
        operations.append(UpdateOne({"_id": document_id}, update))
        if len(operations) >= BACKFILL_BATCH_SIZE:
            collection.bulk_write(operations, ordered=False)
            operations = []
    if operations:
        collection.bulk_write(operations, ordered=False)


def _backfill(collection, updates, set_spec):
    _write_batches(
        collection,
        ((document_id, {"$set": {set_spec: value}}) for document_id, value in updates),
    )


def up(db):
    _backfill(
        db.project,
        (
            (project["_id"], _search_text(project.get("n")))
            for project in db.project.find({}, {"n": 1})
        ),
        "ns",
    )
    _write_batches(
        db.user,
        (
            (
                user["_id"],
                {
                    "$set": {
                        "ns": _search_text(user.get("n")),
                        "asrch": _alias_projection(user.get("aliases")),
                    }
                },
            )
            for user in db.user.find({}, {"n": 1, "aliases": 1})
        ),
    )

    _write_batches(
        db.team_member,
        (
            (
                member["_id"],
                {"$set": {"asrch": _alias_projection(member.get("aliases"))}},
            )
            for member in db.team_member.find({}, {"aliases": 1})
        ),
    )
    _backfill(
        db.project_member,
        (
            (member["_id"], _search_text(member.get("display_name")))
            for member in db.project_member.find({}, {"display_name": 1})
        ),
        "dns",
    )

    _create_named_index(db.project, PROJECT_LIST_INDEX, PROJECT_LIST_KEYS)
    _create_named_index(db.project, PROJECT_TEAM_LIST_INDEX, PROJECT_TEAM_LIST_KEYS)
    _create_named_index(db.project, PROJECT_NAME_INDEX, PROJECT_NAME_KEYS)
    _create_named_index(db.user, USER_NAME_INDEX, USER_NAME_KEYS)
    _create_named_index(db.user, USER_ALIASES_INDEX, USER_ALIASES_KEYS)
    _create_named_index(db.team_member, TEAM_ALIAS_INDEX, TEAM_ALIAS_KEYS)
    _create_named_index(db.project_member, MEMBER_NAME_INDEX, MEMBER_NAME_KEYS)


def down(db):
    raise IrreversibleMigration(
        "search projections are required by the current query model"
    )


def _aliases_match(values, projected):
    return projected == _alias_projection(values)


def verify(db):
    indexes = {
        **db.project.index_information(),
        **db.user.index_information(),
        **db.team_member.index_information(),
        **db.project_member.index_information(),
    }
    expected = {
        PROJECT_LIST_INDEX: PROJECT_LIST_KEYS,
        PROJECT_TEAM_LIST_INDEX: PROJECT_TEAM_LIST_KEYS,
        PROJECT_NAME_INDEX: PROJECT_NAME_KEYS,
        USER_NAME_INDEX: USER_NAME_KEYS,
        USER_ALIASES_INDEX: USER_ALIASES_KEYS,
        TEAM_ALIAS_INDEX: TEAM_ALIAS_KEYS,
        MEMBER_NAME_INDEX: MEMBER_NAME_KEYS,
    }
    if any(
        name not in indexes or tuple(indexes[name].get("key", [])) != keys
        for name, keys in expected.items()
    ):
        return False
    projections_valid = not any(
        project.get("ns") != _search_text(project.get("n"))
        for project in db.project.find({}, {"n": 1, "ns": 1})
    ) and not any(
        member.get("dns") != _search_text(member.get("display_name"))
        for member in db.project_member.find({}, {"display_name": 1, "dns": 1})
    )
    if not projections_valid:
        return False
    return not any(
        user.get("ns") != _search_text(user.get("n"))
        or not _aliases_match(user.get("aliases"), user.get("asrch"))
        for user in db.user.find({}, {"n": 1, "ns": 1, "aliases": 1, "asrch": 1})
    ) and not any(
        not _aliases_match(member.get("aliases"), member.get("asrch"))
        for member in db.team_member.find({}, {"aliases": 1, "asrch": 1})
    )
