"""Small helpers for keeping MongoEngine indexes compatible with migrations."""


def _same_index(index, keys, options):
    if tuple(index.get("key", [])) != tuple(keys):
        return False
    if "unique" in options and bool(index.get("unique")) != bool(options["unique"]):
        return False
    if "sparse" in options and bool(index.get("sparse")) != bool(options["sparse"]):
        return False
    if "partialFilterExpression" in options:
        if index.get("partialFilterExpression") != options["partialFilterExpression"]:
            return False
    return True


def ensure_named_indexes(document_cls, definitions):
    """Ensure indexes without letting MongoEngine regenerate conflicting names.

    MongoEngine drops index names while normalizing ``meta.indexes`` and then
    creates names from the field list.  The identity migrations use stable
    names, so an old generated name with the same key/options must be reused
    instead of being created a second time.
    """

    collection = document_cls._get_collection()
    is_mongomock = collection.__class__.__module__.startswith("mongomock")

    for definition in definitions:
        name, keys, options, fallback = definition
        effective_keys = fallback[0] if is_mongomock and fallback else keys
        effective_options = fallback[1] if is_mongomock and fallback else options
        indexes = collection.index_information()

        if any(
            _same_index(index, effective_keys, effective_options)
            for index in indexes.values()
        ):
            continue

        names_to_drop = {
            index_name
            for index_name, index in indexes.items()
            if index_name == name
            or tuple(index.get("key", [])) == tuple(effective_keys)
        }
        names_to_drop.discard("_id_")
        for index_name in names_to_drop:
            collection.drop_index(index_name)

        collection.create_index(
            effective_keys,
            name=name,
            background=True,
            **effective_options,
        )
