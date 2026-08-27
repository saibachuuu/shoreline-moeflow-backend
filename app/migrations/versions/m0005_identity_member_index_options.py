"""Normalize identity-member subject indexes after the identity migration."""

from app.migrations.runner import IrreversibleMigration


VERSION = "0005"
NAME = "identity_member_index_options"
DESCRIPTION = "Make identity member subject indexes ignore null optional fields"
LEVEL = "AUTO"

PROJECT_MEMBER_INDEXES = (
    (
        "project_member_project_user_v1",
        [("project", 1), ("user", 1)],
        {"unique": True, "partialFilterExpression": {"user": {"$type": "objectId"}}},
        None,
        {},
    ),
    (
        "project_member_project_external_v1",
        [("project", 1), ("external_id", 1)],
        {
            "unique": True,
            "partialFilterExpression": {"external_id": {"$type": "string"}},
        },
        [("external_id", 1)],
        {"unique": True, "sparse": True},
    ),
)


def _same_index(index, keys, options):
    return tuple(index.get("key", [])) == tuple(keys) and all(
        index.get(option) == value
        for option, value in options.items()
    )


def _replace_index(collection, name, keys, options, fallback_keys=None, fallback_options=None):
    if collection.__class__.__module__.startswith("mongomock") and fallback_keys:
        keys = fallback_keys
        options = fallback_options
    indexes = collection.index_information()
    current = indexes.get(name)
    if current is not None and _same_index(current, keys, options):
        return

    # m0004 used a driver-specific sparse fallback for mongomock and older
    # deployments may still have an auto-generated index with the same keys.
    # Drop both the old same-name specification and equivalent key specs before
    # creating the canonical index.  identity_key remains the logical unique
    # constraint, so this replacement cannot permit duplicate subjects.
    names_to_drop = set()
    for index_name, index in indexes.items():
        if index_name == "_id_":
            continue
        if index_name == name or tuple(index.get("key", [])) == tuple(keys):
            names_to_drop.add(index_name)
    for index_name in names_to_drop:
        collection.drop_index(index_name)
    collection.create_index(keys, name=name, background=True, **options)


def up(db):
    # MongoEngine historically persisted nullable optional fields as explicit
    # nulls.  Remove those placeholders before building the sparse fallback.
    db.project_member.update_many(
        {"external_id": None}, {"$unset": {"external_id": ""}}
    )
    for name, keys, options, fallback_keys, fallback_options in PROJECT_MEMBER_INDEXES:
        _replace_index(
            db.project_member,
            name,
            keys,
            options,
            fallback_keys,
            fallback_options,
        )


def down(db):
    raise IrreversibleMigration(
        "identity member index options are required by the runtime model"
    )


def verify(db):
    indexes = db.project_member.index_information()
    return all(
        name in indexes
        and _same_index(
            indexes[name],
            fallback_keys if db.project_member.__class__.__module__.startswith("mongomock") and fallback_keys else keys,
            fallback_options if db.project_member.__class__.__module__.startswith("mongomock") and fallback_keys else options,
        )
        for name, keys, options, fallback_keys, fallback_options in PROJECT_MEMBER_INDEXES
    )
