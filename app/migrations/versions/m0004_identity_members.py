"""Migrate legacy roles and workers into the identity-tag collections.

This migration is deliberately implemented with raw PyMongo documents.  It
must be able to read the pre-identity schema even after runtime code stops
loading legacy relations and the ``Project.workers`` field.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import unicodedata
from uuid import NAMESPACE_URL, uuid5

from bson import ObjectId

from app.migrations.runner import IrreversibleMigration


VERSION = "0004"
NAME = "identity_members"
DESCRIPTION = "Migrate legacy roles and workers into identity members"
LEVEL = "AUTO"

REPORT_COLLECTION = "identity_migration_report"
REPORT_SUMMARY_ID = "identity-v1-summary"
EXTERNAL_NAMESPACE = uuid5(NAMESPACE_URL, "moeflow:identity-migration:v1")

PROJECT_ROLE_TAGS = {
    "creator": "creator",
    "admin": "admin",
    "proofreader": "proofreader",
    "translator": "translator",
    "picture_editor": "typesetter",
    "coordinator": "proofreader",
    "supporter": "translator",
}
TEAM_ROLE_TAGS = {"creator": "creator", "admin": "admin"}
TEAM_ROLE_PRIORITY = {"member": 0, "admin": 1, "creator": 2}

WORKER_TAGS = {
    "provider": "raw_provider",
    "图源": "raw_provider",
    "scan": "scanner",
    "扫图": "scanner",
    "scan_retoucher": "cleaner",
    "修图": "cleaner",
    "translator": "translator",
    "翻译": "translator",
    "proofreader": "proofreader",
    "校对": "proofreader",
    "picture_editor": "typesetter",
    "嵌字": "typesetter",
}

MIGRATION_MAPPING_VERSION = "identity-v1"
PROJECT_STATUS_VALUES = {0, 1, 5}
MEMBER_STATUS_VALUES = {"active", "invited", "removed"}
TEAM_MEMBER_STATUS_VALUES = {"active", "removed"}
MIGRATION_EPOCH = datetime.datetime(1970, 1, 1)
ARTIFACT_ROOT_ENV = "IDENTITY_MIGRATION_ARTIFACT_DIR"
ARTIFACT_BATCH_ENV = "IDENTITY_MIGRATION_BATCH_ID"
ARTIFACT_FILES = (
    "project-members.jsonl",
    "team-members.jsonl",
    "owner-issues.jsonl",
    "role-issues.jsonl",
    "permission-diff.jsonl",
    "workers-audit.jsonl",
    "relation-merge.jsonl",
)


def _ref_id(value):
    """Return the reference id behind a value.

    The legacy schema stores plain relations as ObjectIds, but the
    ``application``/``invitation`` collections use mongoengine
    GenericReferenceFields which persist as ``{"_cls": ..., "_ref": DBRef}``
    documents (and as text ``{"$oid": ...}``/``{"$id": ...}`` objects when the
    data round-trips through exported JSON).  Normalize all of these shapes
    instead of passing a dict through -- an unhashable dict would make the
    ``in`` membership lookups below crash on real production data.
    """

    if value is None:
        return None
    if isinstance(value, dict):
        nested = value.get("_ref")
        if isinstance(nested, dict):
            candidate = nested.get("$id") or nested.get("$oid") or nested.get("id")
        elif nested is not None:
            # BSON DBRef instance: use its .id directly.
            return getattr(nested, "id", nested)
        else:
            candidate = value.get("$oid") or value.get("id")
        if candidate is None:
            return None
        try:
            return ObjectId(str(candidate))
        except (TypeError, ValueError):
            return None
    return getattr(value, "id", value)


def _text(value):
    return value if isinstance(value, str) else ""


def _normalized_name(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _external_id(project_id, name):
    normalized = _normalized_name(name)
    return str(uuid5(EXTERNAL_NAMESPACE, f"{project_id}:{normalized}"))


def _role_code(role, role_codes):
    role_id = _ref_id(role)
    role_data = role_codes.get(role_id)
    if isinstance(role_data, dict):
        return role_data.get("system_code")
    return role_data


def _role_snapshot(role, role_codes):
    role_id = _ref_id(role)
    role_data = role_codes.get(role_id)
    if role_data is None:
        return {"id": role_id}
    if isinstance(role_data, dict):
        return {
            "id": role_id,
            "system_code": role_data.get("system_code"),
            "permissions": list(role_data.get("permissions") or []),
        }
    return {"id": role_id, "system_code": role_data}


def _raw_tag_values(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _normalized_legacy_tags(value):
    return sorted(
        {
            item.strip()
            for item in _raw_tag_values(value)
            if isinstance(item, str) and item.strip()
        }
    )


def _record_legacy_relation(reports, scope, relation, role_codes):
    """Keep legacy relation details in the report without using them as tags."""

    relation_id = relation.get("_id")
    report = reports.setdefault(
        f"{scope}_relation:{relation_id}",
        {"scope": f"{scope}_relation", "source_id": relation_id},
    )
    report["mapping_version"] = MIGRATION_MAPPING_VERSION
    report["role"] = _role_snapshot(relation.get("r"), role_codes)
    raw_tags = _raw_tag_values(relation.get("m_t"))
    if raw_tags:
        report["legacy_tags"] = raw_tags
        _add_issue(
            reports,
            f"{scope}_relation",
            relation_id,
            "legacy_relation_tags",
            raw_tags,
        )


def _record_missing_role(reports, scope, group_id, relation, role_codes, summary):
    role_id = _ref_id(relation.get("r"))
    if role_id in role_codes:
        return
    code = "missing_role_reference" if role_id is None else "unknown_role_reference"
    _add_issue(
        reports,
        scope,
        group_id,
        code,
        {"relation": relation.get("_id"), "role_id": role_id},
    )
    summary["custom_role_issue_count"] += 1


def _add_issue(reports, scope, source_id, code, detail):
    key = f"{scope}:{source_id}"
    report = reports.setdefault(key, {"scope": scope, "source_id": source_id})
    report.setdefault("issues", []).append({"code": code, "detail": detail})


def _valid_datetime(value):
    return value if isinstance(value, datetime.datetime) else None


def _event_time(document, fallback):
    """Use the persisted event time, then the ObjectId time, then a stable fallback."""

    for field in ("c", "m_c", "create_time", "m_e", "edit_time"):
        value = _valid_datetime(document.get(field))
        if value is not None:
            return value
    object_id = document.get("_id")
    generation_time = getattr(object_id, "generation_time", None)
    if isinstance(generation_time, datetime.datetime):
        return generation_time.replace(tzinfo=None)
    return fallback


def _artifact_batch_id(migration_time):
    configured = os.environ.get(ARTIFACT_BATCH_ENV, "").strip()
    if configured:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", configured):
            raise ValueError(
                f"{ARTIFACT_BATCH_ENV} may contain only letters, numbers, '.', '_' and '-'."
            )
        return configured
    return f"{VERSION}-{migration_time.strftime('%Y%m%dT%H%M%S%fZ')}"


def _json_safe(value):
    """Convert Mongo values to deterministic, non-sensitive JSON values."""

    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        values = [_json_safe(item) for item in value]
        return (
            sorted(values, key=lambda item: json.dumps(item, sort_keys=True))
            if isinstance(value, set)
            else values
        )
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _artifact_row(row, batch_id):
    result = {
        "batch_id": batch_id,
        "migration_version": VERSION,
        "mapping_version": MIGRATION_MAPPING_VERSION,
        "result": "migrated",
        **row,
    }
    return _json_safe(result)


def _artifact_before(reports):
    legacy_tags = []
    statuses = []
    roles = []
    source_ids = []
    for report in reports:
        source_ids.append(report.get("source_id"))
        if report.get("role") is not None:
            roles.append(report["role"])
        if report.get("status") is not None:
            statuses.append(report["status"])
        for value in _raw_tag_values(report.get("legacy_tags")):
            if isinstance(value, str) and value.strip():
                legacy_tags.append(value.strip())
    return {
        "source_ids": source_ids,
        "legacy_tags": sorted(set(legacy_tags)),
        "statuses": sorted(set(statuses)),
        "roles": roles,
    }


def _report_issue_codes(report):
    return {
        issue.get("code")
        for issue in report.get("issues", [])
        if isinstance(issue, dict) and issue.get("code")
    }


def _write_jsonl(path, rows):
    content = "".join(
        json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    )
    path.write_text(content, encoding="utf-8", newline="\n")


def _artifact_bundle_is_complete(bundle, batch_id):
    manifest_path = bundle / "manifest.json"
    checksums_path = bundle / "checksums.json"
    if not manifest_path.is_file() or not checksums_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        checksums = json.loads(checksums_path.read_text(encoding="utf-8"))
        expected_files = {"manifest.json", *ARTIFACT_FILES}
        checksum_files = checksums["files"]
        if manifest["batch_id"] != batch_id:
            return False
        if set(manifest["files"]) != set(ARTIFACT_FILES):
            return False
        if checksums["batch_id"] != batch_id or set(checksum_files) != expected_files:
            return False
        return all(
            (bundle / name).is_file()
            and hashlib.sha256((bundle / name).read_bytes()).hexdigest()
            == checksum_files[name]
            for name in expected_files
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _write_migration_artifacts(db, reports, summary, migration_time, batch_id):
    """Write the migration evidence bundle outside the business collections."""

    root = Path(
        os.environ.get(ARTIFACT_ROOT_ENV, "artifacts/identity-migration")
    ).expanduser()
    bundle = root / batch_id
    bundle.mkdir(parents=True, exist_ok=True)
    # A retry after the legacy fields were removed has no source rows left to
    # rebuild the evidence. Preserve an already complete batch instead of
    # replacing it with an empty report.
    if _artifact_bundle_is_complete(bundle, batch_id):
        return

    relation_by_member = {}
    reports_by_member = {}
    for report in reports.values():
        member_keys = [
            report.get("project_member_key"),
            report.get("team_member_key"),
            *report.get("project_member_keys", []),
        ]
        for member_key in dict.fromkeys(item for item in member_keys if item):
            relation_by_member.setdefault(member_key, []).append(
                report.get("source_id")
            )
            reports_by_member.setdefault(member_key, []).append(report)

    project_rows = []
    for member in db.project_member.find({}):
        member_key = (
            f"{_ref_id(member.get('project'))}:u:{_ref_id(member.get('user'))}"
            if member.get("user") is not None
            else f"{_ref_id(member.get('project'))}:e:{member.get('external_id')}"
        )
        project_rows.append(
            _artifact_row(
                {
                    "object_id": member.get("_id"),
                    "scope": "project_member",
                    "project_id": member.get("project"),
                    "user_id": member.get("user"),
                    "external_id": member.get("external_id"),
                    "display_name": member.get("display_name"),
                    "tags": member.get("tags", []),
                    "status": member.get("status"),
                    "version": member.get("version"),
                    "input_source_ids": relation_by_member.get(member_key, []),
                    "source_ids": relation_by_member.get(member_key, []),
                    "before": _artifact_before(reports_by_member.get(member_key, [])),
                    "after": {
                        "status": member.get("status"),
                        "tags": member.get("tags", []),
                        "display_name": member.get("display_name"),
                    },
                    "at": migration_time,
                },
                batch_id,
            )
        )

    team_rows = []
    for member in db.team_member.find({}):
        member_key = f"{_ref_id(member.get('team'))}:u:{_ref_id(member.get('user'))}"
        team_rows.append(
            _artifact_row(
                {
                    "object_id": member.get("_id"),
                    "scope": "team_member",
                    "team_id": member.get("team"),
                    "user_id": member.get("user"),
                    "base_tag": member.get("base_tag"),
                    "tags": member.get("tags", []),
                    "worker_qualifications": member.get("worker_qualifications", []),
                    "aliases": member.get("aliases", []),
                    "status": member.get("status"),
                    "version": member.get("version"),
                    "input_source_ids": relation_by_member.get(member_key, []),
                    "source_ids": relation_by_member.get(member_key, []),
                    "before": _artifact_before(reports_by_member.get(member_key, [])),
                    "after": {
                        "base_tag": member.get("base_tag"),
                        "tags": member.get("tags", []),
                        "worker_qualifications": member.get(
                            "worker_qualifications", []
                        ),
                        "aliases": member.get("aliases", []),
                        "status": member.get("status"),
                    },
                    "at": migration_time,
                },
                batch_id,
            )
        )

    owner_rows = []
    role_rows = []
    permission_rows = []
    worker_rows = []
    merge_rows = []
    for report in reports.values():
        issue_codes = _report_issue_codes(report)
        safe_report = _artifact_row(
            {
                "source_id": report.get("source_id"),
                "scope": report.get("scope"),
                "issues": report.get("issues", []),
                "role": report.get("role"),
                "legacy_status": report.get("legacy_status"),
                "status": report.get("status"),
                "projection": report.get("projection"),
                "project_member_key": report.get("project_member_key"),
                "project_member_keys": report.get("project_member_keys", []),
                "team_member_key": report.get("team_member_key"),
                "mapped_workers": report.get("mapped_workers", []),
                "raw_workers_sha256": report.get("raw_workers_sha256"),
                "raw_workers_length": report.get("raw_workers_length"),
                "before": {
                    "legacy_status": report.get("legacy_status"),
                    "role": report.get("role"),
                    "legacy_tags": report.get("legacy_tags", []),
                },
                "after": {
                    "project_member_key": report.get("project_member_key"),
                    "team_member_key": report.get("team_member_key"),
                    "status": report.get("status") or report.get("projection"),
                    "mapped_tags": report.get("mapped_tags", []),
                    "mapped_base_tag": report.get("mapped_base_tag"),
                },
                "at": migration_time,
            },
            batch_id,
        )
        if any(code.startswith("owner_") for code in issue_codes):
            owner_rows.append(safe_report)
        if report.get("scope") in {"project_relation", "team_relation"} and (
            issue_codes or report.get("role")
        ):
            role_rows.append(safe_report)
            permission_rows.append(
                _artifact_row(
                    {
                        "source_id": report.get("source_id"),
                        "scope": report.get("scope"),
                        "legacy_role": report.get("role"),
                        "mapped_member_key": report.get("project_member_key")
                        or report.get("team_member_key"),
                        "issues": report.get("issues", []),
                        "before": {
                            "role": report.get("role"),
                            "legacy_tags": report.get("legacy_tags", []),
                        },
                        "after": {
                            "member_key": report.get("project_member_key")
                            or report.get("team_member_key"),
                            "tags": report.get("mapped_tags", []),
                            "base_tag": report.get("mapped_base_tag"),
                        },
                        "at": migration_time,
                    },
                    batch_id,
                )
            )
        if (
            report.get("raw_workers_sha256")
            or report.get("mapped_workers")
            or any(
                code.startswith("workers_") or code.startswith("worker_")
                for code in issue_codes
            )
        ):
            worker_rows.append(safe_report)
        if any(
            code
            in {
                "duplicate_member_relation",
                "duplicate_identity_member",
                "invalid_identity_member",
            }
            for code in issue_codes
        ):
            merge_rows.append(safe_report)

    rows_by_name = {
        "project-members.jsonl": project_rows,
        "team-members.jsonl": team_rows,
        "owner-issues.jsonl": owner_rows,
        "role-issues.jsonl": role_rows,
        "permission-diff.jsonl": permission_rows,
        "workers-audit.jsonl": worker_rows,
        "relation-merge.jsonl": merge_rows,
    }
    for name, rows in rows_by_name.items():
        _write_jsonl(bundle / name, rows)

    manifest = {
        "batch_id": batch_id,
        "migration_version": VERSION,
        "mapping_version": MIGRATION_MAPPING_VERSION,
        "generated_at": migration_time,
        "source_report_collection": REPORT_COLLECTION,
        "files": list(ARTIFACT_FILES),
        "summary": summary,
        "counts": {name: len(rows) for name, rows in rows_by_name.items()},
    }
    manifest_path = bundle / "manifest.json"
    manifest_path.write_text(
        json.dumps(_json_safe(manifest), ensure_ascii=True, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )

    checksums = {}
    for path in [manifest_path, *(bundle / name for name in ARTIFACT_FILES)]:
        checksums[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (bundle / "checksums.json").write_text(
        json.dumps(
            _json_safe(
                {
                    "batch_id": batch_id,
                    "migration_version": VERSION,
                    "files": checksums,
                }
            ),
            ensure_ascii=True,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _non_negative_int(value, default=0):
    return value if type(value) is int and value >= 0 else default


def _member_status(value, *, team=False, default="active"):
    allowed = TEAM_MEMBER_STATUS_VALUES if team else MEMBER_STATUS_VALUES
    return value if value in allowed else default


def _status_priority(value, *, team=False):
    if team:
        return {"active": 1, "removed": 0}.get(value, -1)
    return {"active": 2, "invited": 1, "removed": 0}.get(value, -1)


def _member_times(document, fallback=MIGRATION_EPOCH, *, removed=False):
    create_time = _valid_datetime(document.get("create_time")) or fallback
    edit_time = _valid_datetime(document.get("edit_time")) or create_time
    if edit_time < create_time:
        edit_time = create_time
    removed_time = _valid_datetime(document.get("removed_time"))
    if removed and removed_time is None:
        removed_time = edit_time
    if not removed:
        removed_time = None
    return create_time, edit_time, removed_time


def _subject_key(document):
    project_id = _ref_id(document.get("project"))
    user_id = _ref_id(document.get("user"))
    external_id = document.get("external_id")
    if project_id is None:
        return None
    if user_id is not None and external_id:
        return f"{project_id}:u:{user_id}", "conflict"
    if user_id is not None:
        return f"{project_id}:u:{user_id}", "user"
    if isinstance(external_id, str) and external_id:
        return f"{project_id}:e:{external_id}", "external"
    return None


def _merge_existing_documents(db, reports, summary, collection_name, *, team=False):
    """Repair partially written identity records before unique indexes exist."""

    collection = db[collection_name]
    groups = {}
    invalid_ids = []
    for document in collection.find({}):
        if team:
            team_id = _ref_id(document.get("team"))
            user_id = _ref_id(document.get("user"))
            key = (
                (team_id, user_id)
                if team_id is not None and user_id is not None
                else None
            )
        else:
            subject = _subject_key(document)
            key = subject[0] if subject else None
        if key is None:
            invalid_ids.append(document.get("_id"))
            _add_issue(
                reports,
                collection_name,
                document.get("_id"),
                "invalid_identity_member",
                {"team": document.get("team"), "project": document.get("project")},
            )
            continue
        groups.setdefault(key, []).append(document)

    # Invalid subject documents (no project/team scope, or neither user nor
    # external_id) are preserved as evidence instead of being physically
    # deleted: in an irreversible migration a removed document is gone for
    # good, and the issue report already records their presence.  They carry
    # no project scope, so the new partial unique indexes exclude them and
    # keep no uniqueness hole.
    for document_id in invalid_ids:
        if document_id is not None:
            summary["identity_member_reported_invalid_count"] += 1

    for key, documents in groups.items():
        documents.sort(
            key=lambda item: (
                _valid_datetime(item.get("create_time")) or MIGRATION_EPOCH,
                str(item.get("_id")),
            )
        )
        canonical = documents[0]
        merged_tags = set()
        for item in documents:
            merged_tags.update(_normalized_legacy_tags(item.get("tags")))
        statuses = [
            _member_status(item.get("status"), team=team, default=None)
            for item in documents
        ]
        statuses = [item for item in statuses if item is not None]
        status = max(
            statuses,
            key=lambda item: _status_priority(item, team=team),
            default="active",
        )
        version = max(
            (_non_negative_int(item.get("version")) for item in documents),
            default=0,
        )
        create_time, edit_time, removed_time = _member_times(
            canonical,
            removed=status == "removed",
        )
        if team:
            base_tags = [
                item.get("base_tag")
                for item in documents
                if item.get("base_tag") in TEAM_ROLE_PRIORITY
            ]
            base_tag = max(
                base_tags,
                key=lambda item: TEAM_ROLE_PRIORITY[item],
                default="member",
            )
            qualifications = set()
            aliases = set()
            for item in documents:
                qualifications.update(
                    _normalized_legacy_tags(item.get("worker_qualifications"))
                )
                aliases.update(
                    value.strip()
                    for value in _raw_tag_values(item.get("aliases"))
                    if isinstance(value, str) and value.strip()
                )
            invalid_qualifications = qualifications - set(WORKER_TAGS.values())
            if invalid_qualifications:
                _add_issue(
                    reports,
                    collection_name,
                    canonical["_id"],
                    "invalid_worker_qualification",
                    sorted(invalid_qualifications),
                )
                qualifications -= invalid_qualifications
            values = {
                "team": _ref_id(canonical.get("team")),
                "user": _ref_id(canonical.get("user")),
                "base_tag": base_tag,
                "tags": sorted(merged_tags),
                "worker_qualifications": sorted(qualifications),
                "aliases": sorted(aliases),
                "status": status,
            }
        else:
            user_id = _ref_id(canonical.get("user"))
            external_id = canonical.get("external_id")
            display_name = _text(canonical.get("display_name"))
            if not display_name:
                display_name = str(user_id or external_id)
            values = {
                "project": _ref_id(canonical.get("project")),
                "display_name": display_name,
                "tags": sorted(merged_tags),
                "status": status,
                "ik": (
                    f"{_ref_id(canonical.get('project'))}:u:{user_id}"
                    if user_id is not None
                    else f"{_ref_id(canonical.get('project'))}:e:{external_id}"
                ),
            }
            if user_id is not None:
                values["user"] = user_id
            elif external_id:
                values["external_id"] = external_id
        update = {
            **values,
            "create_time": create_time,
            "edit_time": edit_time,
            "version": version,
        }
        if status == "removed":
            update["removed_time"] = removed_time
        unset = {}
        if not team:
            if "user" in values:
                unset["external_id"] = ""
            else:
                unset["user"] = ""
            if status != "removed":
                unset["removed_time"] = ""
        elif status != "removed":
            unset["removed_time"] = ""
        operation = {"$set": update}
        if unset:
            operation["$unset"] = unset
        collection.update_one({"_id": canonical["_id"]}, operation)
        if len(documents) > 1:
            for duplicate in documents[1:]:
                collection.delete_one({"_id": duplicate["_id"]})
            summary["identity_member_duplicate_count"] += len(documents) - 1
            _add_issue(
                reports,
                collection_name,
                canonical["_id"],
                "duplicate_identity_member",
                {"count": len(documents)},
            )


def _project_member_existing(db, values):
    if "user" in values:
        existing = db.project_member.find_one(
            {"project": values["project"], "user": values["user"]}
        )
    else:
        existing = db.project_member.find_one(
            {"project": values["project"], "external_id": values["external_id"]}
        )
    return existing or db.project_member.find_one({"ik": values["ik"]})


def _save_reports(db, reports, summary, migration_time):
    collection = db[REPORT_COLLECTION]
    for report in reports.values():
        report["migration_version"] = VERSION
        report["create_time"] = migration_time
        collection.replace_one(
            {"_id": f"{report['scope']}:{report['source_id']}"},
            report,
            upsert=True,
        )
    collection.replace_one(
        {"_id": REPORT_SUMMARY_ID},
        {
            "_id": REPORT_SUMMARY_ID,
            "migration_version": VERSION,
            "summary": summary,
            "create_time": migration_time,
        },
        upsert=True,
    )


def _upsert_project_member(db, values, tags):
    existing = _project_member_existing(db, values)
    desired_status = values.get("status", "active")
    if desired_status not in MEMBER_STATUS_VALUES:
        desired_status = "active"
    if existing is None:
        create_time = _valid_datetime(values.get("create_time")) or MIGRATION_EPOCH
        edit_time = _valid_datetime(values.get("edit_time")) or create_time
        version = _non_negative_int(values.get("version"))
        final_status = desired_status
        final_tags = sorted(set(_normalized_legacy_tags(tags)))
        update_values = {
            **values,
            "tags": final_tags,
            "status": final_status,
            "create_time": create_time,
            "edit_time": edit_time,
            "version": version,
        }
        if final_status == "removed":
            update_values["removed_time"] = (
                _valid_datetime(values.get("removed_time")) or edit_time
            )
        else:
            update_values.pop("removed_time", None)
        db.project_member.insert_one(update_values)
        return

    current_status = existing.get("status")
    if desired_status == "active" or current_status == "active":
        # A persisted legacy relation/worker projection is authoritative for
        # access.  A stale pending/removed event must not downgrade it.
        final_status = "active"
    elif current_status in MEMBER_STATUS_VALUES:
        final_status = desired_status
    else:
        final_status = desired_status
    create_time = (
        _valid_datetime(existing.get("create_time"))
        or _valid_datetime(values.get("create_time"))
        or MIGRATION_EPOCH
    )
    edit_time = (
        _valid_datetime(existing.get("edit_time"))
        or _valid_datetime(values.get("edit_time"))
        or create_time
    )
    version = _non_negative_int(
        existing.get("version"), _non_negative_int(values.get("version"))
    )
    update_values = {
        "project": values["project"],
        "display_name": _text(existing.get("display_name"))
        or _text(values.get("display_name"))
        or str(values.get("user") or values.get("external_id")),
        "tags": sorted(
            set(_normalized_legacy_tags(existing.get("tags")))
            | set(_normalized_legacy_tags(tags))
        ),
        "status": final_status,
        "create_time": create_time,
        "edit_time": edit_time,
        "version": version,
        "ik": values["ik"],
    }
    if "user" in values:
        update_values["user"] = values["user"]
        unset = {"external_id": ""}
    else:
        update_values["external_id"] = values["external_id"]
        unset = {"user": ""}
    if final_status == "removed":
        update_values["removed_time"] = (
            _valid_datetime(existing.get("removed_time"))
            or _valid_datetime(values.get("removed_time"))
            or edit_time
        )
    else:
        unset["removed_time"] = ""
    db.project_member.update_one(
        {"_id": existing["_id"]},
        {"$set": update_values, "$unset": unset},
    )


def _upsert_team_member(db, values, base_tag):
    existing = db.team_member.find_one({"team": values["team"], "user": values["user"]})
    if existing is None:
        values = {
            **values,
            "base_tag": base_tag if base_tag in TEAM_ROLE_PRIORITY else "member",
            "tags": _normalized_legacy_tags(values.get("tags")),
            "worker_qualifications": _normalized_legacy_tags(
                values.get("worker_qualifications")
            ),
            "aliases": [
                item.strip()
                for item in _raw_tag_values(values.get("aliases"))
                if isinstance(item, str) and item.strip()
            ],
            "status": values.get("status")
            if values.get("status") in TEAM_MEMBER_STATUS_VALUES
            else "active",
            "create_time": _valid_datetime(values.get("create_time"))
            or MIGRATION_EPOCH,
            "edit_time": _valid_datetime(values.get("edit_time"))
            or _valid_datetime(values.get("create_time"))
            or MIGRATION_EPOCH,
            "version": _non_negative_int(values.get("version")),
        }
        if values["status"] == "removed":
            values["removed_time"] = (
                _valid_datetime(values.get("removed_time")) or values["edit_time"]
            )
        db.team_member.insert_one(values)
        return

    old_tag = existing.get("base_tag", "member")
    if TEAM_ROLE_PRIORITY.get(old_tag, 0) > TEAM_ROLE_PRIORITY.get(base_tag, 0):
        base_tag = old_tag
    status = existing.get("status")
    if status not in TEAM_MEMBER_STATUS_VALUES:
        status = (
            values.get("status")
            if values.get("status") in TEAM_MEMBER_STATUS_VALUES
            else "active"
        )
    create_time = (
        _valid_datetime(existing.get("create_time"))
        or _valid_datetime(values.get("create_time"))
        or MIGRATION_EPOCH
    )
    edit_time = (
        _valid_datetime(existing.get("edit_time"))
        or _valid_datetime(values.get("edit_time"))
        or create_time
    )
    update_values = {
        "team": values["team"],
        "user": values["user"],
        "base_tag": base_tag if base_tag in TEAM_ROLE_PRIORITY else "member",
        "tags": sorted(
            set(_normalized_legacy_tags(values.get("tags")))
            | set(_normalized_legacy_tags(existing.get("tags")))
        ),
        "worker_qualifications": _normalized_legacy_tags(
            existing.get("worker_qualifications")
            if existing.get("worker_qualifications") is not None
            else values.get("worker_qualifications")
        ),
        "aliases": [
            item.strip()
            for item in _raw_tag_values(
                existing.get("aliases")
                if existing.get("aliases") is not None
                else values.get("aliases")
            )
            if isinstance(item, str) and item.strip()
        ],
        "status": status,
        "create_time": create_time,
        "edit_time": edit_time,
        "version": _non_negative_int(
            existing.get("version"), _non_negative_int(values.get("version"))
        ),
    }
    unset = {}
    if status == "removed":
        update_values["removed_time"] = (
            _valid_datetime(existing.get("removed_time"))
            or _valid_datetime(values.get("removed_time"))
            or edit_time
        )
    else:
        unset["removed_time"] = ""
    update = {"$set": update_values}
    if unset:
        update["$unset"] = unset
    db.team_member.update_one({"_id": existing["_id"]}, update)


def _ensure_index(collection, keys, name, *, unique=False, partial_filter=None):
    use_sparse_fallback = (
        partial_filter is not None
        and collection.__class__.__module__.startswith("mongomock")
    )
    effective_keys = list(keys)
    if use_sparse_fallback and any(field == "external_id" for field, _ in keys):
        # mongomock treats a missing trailing field in a compound sparse
        # index as a real null value.  ``external_id`` is generated globally
        # by the service, so a sparse single-field index is equivalent for the
        # test driver while the production path keeps the documented scoped
        # compound partial index.
        effective_keys = [("external_id", 1)]
    key_spec = tuple(effective_keys)
    for index_name, index in collection.index_information().items():
        if tuple(index.get("key", [])) != key_spec:
            continue
        current_partial = index.get("partialFilterExpression")
        current_sparse = bool(index.get("sparse"))
        matches = bool(index.get("unique")) == unique and (
            current_partial == partial_filter
            or (use_sparse_fallback and current_sparse)
        )
        if matches and index_name == name:
            return
        if index_name != "_id_":
            collection.drop_index(index_name)
    kwargs = {"name": name, "unique": unique, "background": True}
    if partial_filter is not None:
        if use_sparse_fallback:
            # mongomock currently accepts the partial-index option but does
            # not apply its filter when checking uniqueness.  All migrated
            # optional subjects are absent rather than null, so sparse is the
            # equivalent fallback for the test driver.
            kwargs["sparse"] = True
        else:
            kwargs["partialFilterExpression"] = partial_filter
    collection.create_index(effective_keys, **kwargs)


def _migrate_project_status(db, project, reports, summary, fallback_time):
    project_id = project["_id"]
    old_status = project.get("st", 0)
    new_status = {0: 0, 1: 1, 5: 5}.get(old_status)
    if old_status == 2:
        new_status = 0
        _add_issue(
            reports,
            "project",
            project_id,
            "legacy_finish_plan",
            "PLAN_FINISH was normalized to NORMAL; review the pending plan separately.",
        )
        summary["status_plan_count"] += 1
    elif old_status == 3:
        new_status = 0
        _add_issue(
            reports,
            "project",
            project_id,
            "legacy_delete_plan",
            "PLAN_DELETE was normalized to NORMAL; no destructive delete was performed.",
        )
        summary["status_plan_count"] += 1
    elif new_status is None:
        new_status = 0
        _add_issue(
            reports,
            "project",
            project_id,
            "unknown_project_status",
            repr(old_status),
        )
        summary["status_issue_count"] += 1

    update = {
        "st": new_status,
        "stv": _non_negative_int(project.get("stv")),
        "ouv": _non_negative_int(project.get("ouv")),
    }
    # ``ft`` belongs to the destructive legacy finish flow.  It must not make
    # a historical CLEARED project look like the new non-destructive
    # COMPLETED state.  Only an already new-style completed project may use it
    # as a fallback timestamp.
    unset = {"pft": "", "pdt": "", "ft": ""}
    if new_status == 5:
        update["ctm"] = (
            _valid_datetime(project.get("ctm"))
            or _valid_datetime(project.get("ft"))
            or fallback_time
        )
    elif "ctm" in project:
        unset["ctm"] = ""
    db.project.update_one(
        {"_id": project_id},
        {"$set": update, "$unset": unset},
    )
    summary["projects"] += 1


def _migrate_workers(db, project, reports, external_members, summary, fallback_time):
    raw = project.get("w")
    if raw is None:
        return
    project_id = project["_id"]
    report = reports.setdefault(
        f"project:{project_id}",
        {"scope": "project", "source_id": project_id},
    )
    raw_text = raw if isinstance(raw, str) else repr(raw)
    report["mapping_version"] = MIGRATION_MAPPING_VERSION
    report["raw_workers_sha256"] = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    report["raw_workers_length"] = len(raw_text)
    summary["workers_projects"] += 1
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError) as error:
        _add_issue(reports, "project", project_id, "workers_parse_error", str(error))
        summary["workers_issue_count"] += 1
        return
    if not isinstance(parsed, dict):
        _add_issue(
            reports,
            "project",
            project_id,
            "workers_root_not_object",
            type(parsed).__name__,
        )
        summary["workers_issue_count"] += 1
        return

    report["worker_keys"] = sorted(str(key) for key in parsed)
    report.setdefault("mapped_workers", [])

    for raw_tag, names in parsed.items():
        target_tag = WORKER_TAGS.get(raw_tag)
        if target_tag is None:
            _add_issue(reports, "project", project_id, "unknown_worker_tag", raw_tag)
            summary["workers_issue_count"] += 1
            continue
        if not isinstance(names, list):
            _add_issue(
                reports, "project", project_id, "worker_names_not_array", raw_tag
            )
            summary["workers_issue_count"] += 1
            continue
        for name in names:
            if not isinstance(name, str) or not name.strip():
                _add_issue(
                    reports, "project", project_id, "invalid_worker_name", repr(name)
                )
                summary["workers_issue_count"] += 1
                continue
            display_name = name.strip()
            external_key = (project_id, _normalized_name(display_name))
            member = external_members.setdefault(
                external_key,
                {
                    "project": project_id,
                    "external_id": _external_id(project_id, display_name),
                    "display_name": display_name,
                    "tags": set(),
                    "create_time": _valid_datetime(project.get("m_c")) or fallback_time,
                    "edit_time": _valid_datetime(project.get("m_e"))
                    or _valid_datetime(project.get("m_c"))
                    or fallback_time,
                },
            )
            if not member["tags"]:
                summary["external_members"] += 1
            member["tags"].add(target_tag)
            report["mapped_workers"].append(
                {
                    "source_tag": raw_tag,
                    "tag": target_tag,
                    "display_name": display_name,
                    "project_member_key": (f"{project_id}:e:{member['external_id']}"),
                }
            )
            report.setdefault("project_member_keys", []).append(
                f"{project_id}:e:{member['external_id']}"
            )
            summary["worker_values"] += 1


def _join_process_status(value):
    return {1: "pending", 2: "active", 3: "removed"}.get(value)


def _record_join_process_event(
    reports,
    events,
    document,
    *,
    kind,
    project_by_id,
    team_by_id,
    users,
    role_codes,
    summary,
    fallback_time,
):
    group_id = _ref_id(document.get("g"))
    user_id = _ref_id(document.get("u"))
    status = _join_process_status(document.get("s"))
    source_id = document.get("_id")
    scope = "invitation" if kind == "invitation" else "application"
    report = reports.setdefault(
        f"{scope}:{source_id}",
        {"scope": scope, "source_id": source_id},
    )
    report.update(
        {
            "mapping_version": MIGRATION_MAPPING_VERSION,
            "group_id": group_id,
            "user_id": user_id,
            "legacy_status": document.get("s"),
            "status": status,
        }
    )
    if kind == "invitation":
        report["role"] = _role_snapshot(document.get("r"), role_codes)
    if status is None:
        _add_issue(
            reports, scope, source_id, "unknown_join_process_status", document.get("s")
        )
        summary["join_process_issue_count"] += 1
        return
    summary_key = (
        f"allowed_{kind}_count"
        if status == "active"
        else f"denied_{kind}_count"
        if status == "removed"
        else f"pending_{kind}_count"
    )
    summary[summary_key] += 1
    if (
        group_id not in project_by_id and group_id not in team_by_id
    ) or user_id not in users:
        _add_issue(
            reports,
            scope,
            source_id,
            "dangling_reference",
            {"project_or_team": group_id, "user": user_id},
        )
        summary["join_process_issue_count"] += 1
        return
    project = project_by_id.get(group_id)
    # Team invitations/applications remain in their original collections and
    # do not create project members.  They are still audited for a complete
    # migration report.
    if project is None:
        report["projection"] = "team_scope_no_project_member"
        return
    event_time = _event_time(document, fallback_time)
    event = {
        "kind": kind,
        "status": status,
        "time": event_time,
        "source_id": source_id,
    }
    events.setdefault((group_id, user_id), []).append(event)


def _project_join_process_members(
    db,
    reports,
    project_members,
    events,
    project_by_id,
    users,
    summary,
    fallback_time,
):
    """Project the final historical invitation/application state.

    Existing legacy relations are authoritative for an active membership.  A
    pending invitation becomes ``invited``; an accepted join without a legacy
    relation becomes an access-only ``active`` member.  Pending applications
    never become members because the old application flow did not grant access.
    A denied/cancelled application is retained as a removed logical member so
    a later rejoin can reuse its identity.
    """

    for key, event_list in events.items():
        project_id, user_id = key
        if key in project_members:
            report_status = "active_relation"
            for event in event_list:
                report = reports.get(
                    f"{event['kind']}:{event['source_id']}",
                    {"scope": event["kind"], "source_id": event["source_id"]},
                )
                report["projection"] = report_status
                report["project_member_key"] = f"{project_id}:u:{user_id}"
                reports[f"{event['kind']}:{event['source_id']}"] = report
            continue

        event_list.sort(key=lambda item: (item["time"], str(item["source_id"])))
        final_event = event_list[-1]
        status = final_event["status"]
        member_key = f"{project_id}:u:{user_id}"
        user_name = users[user_id]
        if status == "pending" and final_event["kind"] == "application":
            # Pending applications never granted access in the old runtime;
            # retaining only the report is the only projection that preserves
            # both that behavior and the new invited-member invariant.
            projection = "pending_application_no_member"
        else:
            source_time = final_event["time"] or fallback_time
            values = {
                "project": project_id,
                "user": user_id,
                "ik": member_key,
                "display_name": user_name or str(user_id),
                "status": "invited" if status == "pending" else status,
                "create_time": source_time,
                "edit_time": source_time,
                "version": 0,
            }
            project_members[key] = {
                "project": project_id,
                "user": user_id,
                "display_name": values["display_name"],
                "tags": set(),
                "create_time": source_time,
                "edit_time": source_time,
                "status": values["status"],
                "version": 0,
                "removed_time": source_time if values["status"] == "removed" else None,
                "relation_ids": [],
            }
            summary["project_members_from_join_process"] += 1
            projection = f"project_member_{values['status']}"
        for event in event_list:
            report = reports.get(
                f"{event['kind']}:{event['source_id']}",
                {"scope": event["kind"], "source_id": event["source_id"]},
            )
            report["projection"] = projection
            if projection != "pending_application_no_member":
                report["project_member_key"] = member_key
            reports[f"{event['kind']}:{event['source_id']}"] = report


def up(db):
    """Backfill all identity projections and remove consumed legacy fields."""
    reports = {}
    migration_time = datetime.datetime.utcnow()
    batch_id = _artifact_batch_id(migration_time)
    summary = {
        "batch_id": batch_id,
        "projects": 0,
        "teams": 0,
        "project_members": 0,
        "team_members": 0,
        "external_members": 0,
        "workers_projects": 0,
        "worker_values": 0,
        "workers_issue_count": 0,
        "project_relation_count": 0,
        "project_relation_duplicate_count": 0,
        "team_relation_count": 0,
        "team_relation_duplicate_count": 0,
        "legacy_relation_tag_count": 0,
        "status_plan_count": 0,
        "status_issue_count": 0,
        "owner_issue_count": 0,
        "owner_repaired_count": 0,
        "owner_missing_count": 0,
        "owner_conflict_count": 0,
        "owner_invalid_count": 0,
        "custom_role_issue_count": 0,
        "identity_member_duplicate_count": 0,
        "identity_member_repaired_count": 0,
        "identity_member_reported_invalid_count": 0,
        "invitation_count": 0,
        "application_count": 0,
        "pending_invitation_count": 0,
        "allowed_invitation_count": 0,
        "denied_invitation_count": 0,
        "pending_application_count": 0,
        "allowed_application_count": 0,
        "denied_application_count": 0,
        "project_members_from_join_process": 0,
        "join_process_issue_count": 0,
    }

    # A crashed or manually interrupted first attempt may have left partially
    # shaped identity documents.  Repair and merge those before creating the
    # logical unique indexes below.
    _merge_existing_documents(db, reports, summary, "project_member", team=False)
    _merge_existing_documents(db, reports, summary, "team_member", team=True)

    users = {item["_id"]: _text(item.get("n")) for item in db.user.find({}, {"n": 1})}
    project_roles = {
        item["_id"]: {
            "system_code": item.get("m_o"),
            "permissions": item.get("m_p") or [],
        }
        for item in db.project_role.find({}, {"m_o": 1, "m_p": 1})
    }
    team_roles = {
        item["_id"]: {
            "system_code": item.get("m_o"),
            "permissions": item.get("m_p") or [],
        }
        for item in db.team_role.find({}, {"m_o": 1, "m_p": 1})
    }
    projects = list(db.project.find({}))
    project_by_id = {item["_id"]: item for item in projects}
    teams = list(db.team.find({}))
    team_by_id = {item["_id"]: item for item in teams}

    project_members = {}
    owner_candidates = {}
    for relation in db.project_user_relation.find({}):
        summary["project_relation_count"] += 1
        _record_legacy_relation(reports, "project", relation, project_roles)
        if relation.get("m_t"):
            summary["legacy_relation_tag_count"] += 1
        project_id = _ref_id(relation.get("g"))
        user_id = _ref_id(relation.get("u"))
        role_code = _role_code(relation.get("r"), project_roles)
        if project_id not in project_by_id or user_id not in users:
            _add_issue(
                reports,
                "project_relation",
                relation.get("_id"),
                "dangling_reference",
                {"project": project_id, "user": user_id},
            )
            continue
        if relation.get("r") is None or _ref_id(relation.get("r")) not in project_roles:
            _record_missing_role(
                reports, "project", project_id, relation, project_roles, summary
            )
        tags = set()
        mapped_tag = PROJECT_ROLE_TAGS.get(role_code)
        if mapped_tag:
            tags.add(mapped_tag)
        else:
            _add_issue(
                reports, "project", project_id, "unmapped_project_role", role_code
            )
            summary["custom_role_issue_count"] += 1
        key = (project_id, user_id)
        member = project_members.setdefault(
            key,
            {
                "project": project_id,
                "user": user_id,
                "display_name": users[user_id] or str(user_id),
                "tags": set(),
                "create_time": _event_time(relation, migration_time),
                "edit_time": _valid_datetime(relation.get("m_e"))
                or _event_time(relation, migration_time),
                "status": "active",
                "version": 0,
                "removed_time": None,
                "relation_ids": [],
            },
        )
        if member["relation_ids"]:
            summary["project_relation_duplicate_count"] += 1
            _add_issue(
                reports,
                "project_relation",
                relation.get("_id"),
                "duplicate_member_relation",
                {"canonical_relation": member["relation_ids"][0], "member": key},
            )
        member["relation_ids"].append(relation.get("_id"))
        member["tags"].update(tags)
        relation_report = reports.get(f"project_relation:{relation.get('_id')}")
        if relation_report is not None:
            relation_report["project_member_key"] = f"{project_id}:u:{user_id}"
            relation_report["mapped_tags"] = sorted(tags)
        if role_code == "creator":
            owner_candidates.setdefault(project_id, set()).add(user_id)

    join_process_events = {}
    for invitation in db.invitation.find({}):
        summary["invitation_count"] += 1
        invitation_group_id = _ref_id(invitation.get("g"))
        _record_join_process_event(
            reports,
            join_process_events,
            invitation,
            kind="invitation",
            project_by_id=project_by_id,
            team_by_id=team_by_id,
            users=users,
            role_codes=(
                project_roles if invitation_group_id in project_by_id else team_roles
            ),
            summary=summary,
            fallback_time=migration_time,
        )
    for application in db.application.find({}):
        summary["application_count"] += 1
        application_group_id = _ref_id(application.get("g"))
        _record_join_process_event(
            reports,
            join_process_events,
            application,
            kind="application",
            project_by_id=project_by_id,
            team_by_id=team_by_id,
            users=users,
            role_codes=(
                project_roles if application_group_id in project_by_id else team_roles
            ),
            summary=summary,
            fallback_time=migration_time,
        )
    _project_join_process_members(
        db,
        reports,
        project_members,
        join_process_events,
        project_by_id,
        users,
        summary,
        migration_time,
    )

    for project, members in project_members.items():
        project_id, user_id = project
        _upsert_project_member(
            db,
            {
                "project": project_id,
                "user": user_id,
                "ik": f"{project_id}:u:{user_id}",
                "display_name": members["display_name"],
                "status": members["status"],
                "create_time": members["create_time"],
                "edit_time": members["edit_time"],
                "version": members["version"],
                "removed_time": members["removed_time"],
            },
            members["tags"],
        )
        summary["project_members"] += 1

    external_members = {}
    for project in projects:
        _migrate_project_status(db, project, reports, summary, migration_time)
        _migrate_workers(
            db, project, reports, external_members, summary, migration_time
        )

    for (project_id, _), member in external_members.items():
        external_id = member["external_id"]
        _upsert_project_member(
            db,
            {
                "project": project_id,
                "external_id": external_id,
                "ik": f"{project_id}:e:{external_id}",
                "display_name": member["display_name"],
                "status": "active",
                "create_time": member["create_time"],
                "edit_time": member["edit_time"],
                "version": 0,
            },
            member["tags"],
        )

    # Creator-less projects are repaired by binding the team creator, falling
    # back to the site creator (the earliest user) so the project is never left
    # without an owner.  Team creators are derived from the legacy team
    # relations before the team rows themselves are projected.
    team_creator_by_team = {}
    for relation in db.team_user_relation.find({}, {"g": 1, "u": 1, "r": 1}):
        team_id = _ref_id(relation.get("g"))
        user_id = _ref_id(relation.get("u"))
        if team_id is None or user_id is None:
            continue
        role_code = _role_code(relation.get("r"), team_roles)
        if TEAM_ROLE_TAGS.get(role_code) == "creator":
            team_creator_by_team.setdefault(team_id, user_id)
    earliest_user = db.user.find_one({}, {"_id": 1}, sort=[("_id", 1)])
    site_creator_id = earliest_user["_id"] if earliest_user else None

    for project_id, project in project_by_id.items():
        candidates = owner_candidates.get(project_id, set())
        current_owner = _ref_id(project.get("ou"))
        if current_owner is not None:
            if current_owner not in users:
                _add_issue(
                    reports,
                    "project",
                    project_id,
                    "owner_invalid_user",
                    current_owner,
                )
                summary["owner_invalid_count"] += 1
                summary["owner_issue_count"] += 1
            elif (project_id, current_owner) not in project_members:
                _add_issue(
                    reports,
                    "project",
                    project_id,
                    "owner_not_in_creator_relations",
                    current_owner,
                )
                summary["owner_issue_count"] += 1
            elif current_owner not in candidates:
                _add_issue(
                    reports,
                    "project",
                    project_id,
                    "owner_not_in_creator_relations",
                    current_owner,
                )
                summary["owner_issue_count"] += 1
            if len(candidates) > 1:
                _add_issue(
                    reports,
                    "project",
                    project_id,
                    "owner_candidate_conflict",
                    [str(item) for item in sorted(candidates, key=str)],
                )
                summary["owner_conflict_count"] += 1
                summary["owner_issue_count"] += 1
        elif len(candidates) == 1:
            db.project.update_one(
                {"_id": project_id},
                {"$set": {"ou": next(iter(candidates)), "ouv": project.get("ouv", 0)}},
            )
            summary["owner_repaired_count"] += 1
        elif not candidates:
            fallback_owner = team_creator_by_team.get(_ref_id(project.get("t")))
            fallback_source = "team_creator"
            if fallback_owner is None or fallback_owner not in users:
                fallback_owner = site_creator_id
                fallback_source = "site_creator"
            if fallback_owner is None or fallback_owner not in users:
                _add_issue(reports, "project", project_id, "owner_missing", None)
                summary["owner_missing_count"] += 1
                summary["owner_issue_count"] += 1
                continue
            db.project.update_one(
                {"_id": project_id},
                {"$set": {"ou": fallback_owner, "ouv": project.get("ouv", 0)}},
            )
            _upsert_project_member(
                db,
                {
                    "project": project_id,
                    "user": fallback_owner,
                    "ik": f"{project_id}:u:{fallback_owner}",
                    "display_name": users[fallback_owner] or str(fallback_owner),
                    "status": "active",
                    "create_time": migration_time,
                    "edit_time": migration_time,
                    "version": 0,
                    "removed_time": None,
                },
                {"creator"},
            )
            _add_issue(
                reports,
                "project",
                project_id,
                f"owner_bound_to_{fallback_source}",
                fallback_owner,
            )
            summary["owner_repaired_count"] += 1
        else:
            _add_issue(
                reports,
                "project",
                project_id,
                "owner_candidate_conflict",
                [str(item) for item in sorted(candidates, key=str)],
            )
            summary["owner_conflict_count"] += 1
            summary["owner_issue_count"] += 1

    for team in teams:
        team_id = team["_id"]
        summary["teams"] += 1
        db.identity_tag_policy.update_one(
            {"team": team_id},
            {
                "$setOnInsert": {
                    "team": team_id,
                    "team_tags": {},
                    "project_tags": {},
                    "version": 0,
                    "create_time": migration_time,
                    "edit_time": migration_time,
                }
            },
            upsert=True,
        )

    team_members = {}
    for relation in db.team_user_relation.find({}):
        summary["team_relation_count"] += 1
        _record_legacy_relation(reports, "team", relation, team_roles)
        if relation.get("m_t"):
            summary["legacy_relation_tag_count"] += 1
        team_id = _ref_id(relation.get("g"))
        user_id = _ref_id(relation.get("u"))
        role_code = _role_code(relation.get("r"), team_roles)
        if team_id not in team_by_id or user_id not in users:
            _add_issue(
                reports,
                "team_relation",
                relation.get("_id"),
                "dangling_reference",
                {"team": team_id, "user": user_id},
            )
            continue
        if relation.get("r") is None or _ref_id(relation.get("r")) not in team_roles:
            _record_missing_role(
                reports, "team", team_id, relation, team_roles, summary
            )
        base_tag = TEAM_ROLE_TAGS.get(role_code, "member")
        if (
            role_code
            and role_code not in TEAM_ROLE_TAGS
            and role_code not in {"member", "senior", "beginner"}
        ):
            _add_issue(reports, "team", team_id, "unmapped_team_role", role_code)
            summary["custom_role_issue_count"] += 1
        key = (team_id, user_id)
        current = team_members.setdefault(
            key,
            {
                "team": team_id,
                "user": user_id,
                "base_tag": "member",
                "tags": [],
                "worker_qualifications": [],
                "aliases": [],
                "version": 0,
                "status": "active",
                "create_time": _event_time(relation, migration_time),
                "edit_time": _valid_datetime(relation.get("m_e"))
                or _event_time(relation, migration_time),
                "relation_ids": [],
            },
        )
        if current["relation_ids"]:
            summary["team_relation_duplicate_count"] += 1
            _add_issue(
                reports,
                "team_relation",
                relation.get("_id"),
                "duplicate_member_relation",
                {"canonical_relation": current["relation_ids"][0], "member": key},
            )
        current["relation_ids"].append(relation.get("_id"))
        relation_report = reports.get(f"team_relation:{relation.get('_id')}")
        if relation_report is not None:
            relation_report["team_member_key"] = f"{team_id}:u:{user_id}"
            relation_report["mapped_base_tag"] = base_tag
        # ``m_t`` is a legacy relation annotation, not an identity tag.  It
        # is retained only in the relation report above; importing it here
        # would turn stale role metadata into live permissions or labels.
        if TEAM_ROLE_PRIORITY[base_tag] > TEAM_ROLE_PRIORITY[current["base_tag"]]:
            current["base_tag"] = base_tag

    for (team_id, user_id), member in team_members.items():
        # Relation IDs are migration evidence and must not leak into the
        # business projection.
        member_values = {
            key: value for key, value in member.items() if key != "relation_ids"
        }
        _upsert_team_member(db, member_values, member["base_tag"])
        summary["team_members"] += 1

    for project in projects:
        project_id = project["_id"]
        active_count = db.project_member.count_documents(
            {"project": project_id, "status": "active"}
        )
        db.project.update_one(
            {"_id": project_id}, {"$set": {"m_uc": active_count}, "$unset": {"w": ""}}
        )
    for team in teams:
        team_id = team["_id"]
        active_count = db.team_member.count_documents(
            {"team": team_id, "status": "active"}
        )
        db.team.update_one({"_id": team_id}, {"$set": {"m_uc": active_count}})

    _ensure_index(
        db.project_member,
        [("project", 1), ("user", 1)],
        "project_member_project_user_v1",
        unique=True,
        partial_filter={"user": {"$exists": True}},
    )
    _ensure_index(
        db.project_member,
        [("project", 1), ("external_id", 1)],
        "project_member_project_external_v1",
        unique=True,
        partial_filter={"external_id": {"$exists": True}},
    )
    _ensure_index(
        db.project_member,
        [("ik", 1)],
        "project_member_identity_key_v1",
        unique=True,
    )
    _ensure_index(
        db.team_member,
        [("team", 1), ("user", 1)],
        "team_member_team_user_v1",
        unique=True,
    )
    _ensure_index(
        db.project_member, [("project", 1), ("status", 1)], "project_member_status_v1"
    )
    _ensure_index(
        db.project_member, [("project", 1), ("tags", 1)], "project_member_tags_v1"
    )
    _ensure_index(
        db.project_member,
        [("project", 1), ("display_name", 1)],
        "project_member_display_name_v1",
    )

    for report in reports.values():
        report["batch_id"] = batch_id
    _save_reports(db, reports, summary, migration_time)
    _write_migration_artifacts(
        db,
        reports,
        summary,
        migration_time,
        batch_id,
    )


def down(db):
    raise IrreversibleMigration(
        "identity_members is irreversible; restore the database and object-storage snapshots"
    )


def verify(db):
    if db.project.count_documents({"st": {"$nin": sorted(PROJECT_STATUS_VALUES)}}):
        return False
    if db.project.count_documents({"stv": {"$not": {"$type": "int"}}}):
        return False
    if db.project.count_documents({"ouv": {"$not": {"$type": "int"}}}):
        return False
    if db.project.count_documents({"stv": {"$lt": 0}}):
        return False
    if db.project.count_documents({"ouv": {"$lt": 0}}):
        return False
    if db.project.count_documents({"w": {"$exists": True}}):
        return False
    if db.project.count_documents({"pft": {"$exists": True}}):
        return False
    if db.project.count_documents({"pdt": {"$exists": True}}):
        return False
    if db.project.count_documents({"ft": {"$exists": True}}):
        return False
    for project in db.project.find({}, {"_id": 1, "st": 1, "ctm": 1, "m_uc": 1}):
        if project.get("st") == 5:
            if not isinstance(project.get("ctm"), datetime.datetime):
                return False
        elif "ctm" in project:
            return False
        if type(project.get("m_uc")) is not int or project["m_uc"] < 0:
            return False
        if project["m_uc"] != db.project_member.count_documents(
            {"project": project["_id"], "status": "active"}
        ):
            # Member counts are recalculated from the new collection; verify
            # must assert the recalculated value, not just its type.
            return False

    for member in db.project_member.find({}):
        project_id = _ref_id(member.get("project"))
        user_id = _ref_id(member.get("user"))
        external_id = member.get("external_id")
        has_user = user_id is not None
        has_external = isinstance(external_id, str) and bool(external_id)
        if project_id is None or has_user == has_external:
            return False
        expected_key = (
            f"{project_id}:u:{user_id}" if has_user else f"{project_id}:e:{external_id}"
        )
        if member.get("ik") != expected_key:
            return False
        if (
            not isinstance(member.get("display_name"), str)
            or not member["display_name"].strip()
        ):
            return False
        if len(member["display_name"]) > 140:
            # Runtime ProjectMember.display_name rejects over-limit names; a
            # migrated value above the limit would fail every later save.
            return False
        if member.get("status") not in MEMBER_STATUS_VALUES:
            return False
        if not isinstance(member.get("tags"), list) or any(
            not isinstance(tag, str) or not tag.strip() for tag in member["tags"]
        ):
            return False
        if (
            not isinstance(member.get("create_time"), datetime.datetime)
            or not isinstance(member.get("edit_time"), datetime.datetime)
            or type(member.get("version")) is not int
            or member["version"] < 0
        ):
            return False
        if member.get("status") == "removed":
            if not isinstance(member.get("removed_time"), datetime.datetime):
                return False
        elif "removed_time" in member and member.get("removed_time") is not None:
            return False

    for team in db.team.find({}, {"_id": 1, "m_uc": 1}):
        if type(team.get("m_uc")) is not int or team["m_uc"] < 0:
            return False
        if team["m_uc"] != db.team_member.count_documents(
            {"team": team["_id"], "status": "active"}
        ):
            return False
    for member in db.team_member.find({}):
        if _ref_id(member.get("team")) is None or _ref_id(member.get("user")) is None:
            return False
        if member.get("base_tag") not in TEAM_ROLE_PRIORITY:
            return False
        if member.get("status") not in TEAM_MEMBER_STATUS_VALUES:
            return False
        if (
            not isinstance(member.get("tags"), list)
            or not isinstance(member.get("worker_qualifications"), list)
            or not isinstance(member.get("aliases"), list)
            or any(
                not isinstance(tag, str) or not tag.strip() for tag in member["tags"]
            )
            or any(
                qualification not in set(WORKER_TAGS.values())
                for qualification in member["worker_qualifications"]
            )
            or any(
                not isinstance(alias, str) or not alias.strip()
                for alias in member["aliases"]
            )
        ):
            return False
        if (
            not isinstance(member.get("create_time"), datetime.datetime)
            or not isinstance(member.get("edit_time"), datetime.datetime)
            or type(member.get("version")) is not int
            or member["version"] < 0
        ):
            return False
        if member.get("status") == "removed":
            if not isinstance(member.get("removed_time"), datetime.datetime):
                return False
        elif "removed_time" in member and member.get("removed_time") is not None:
            return False

    def has_index(collection, keys, *, unique=False, partial_filter=None):
        use_sparse_fallback = (
            partial_filter is not None
            and collection.__class__.__module__.startswith("mongomock")
        )
        # ``partial_filter`` may name several accepted shapes.  run_pending
        # verifies right after 0004's up(), when the subject indexes still
        # carry the $exists partial filters, while a post-0005 database has
        # the $type replacements — verify must accept both, because both are
        # the canonical state of their respective migration stage.
        accepted_partials = (
            tuple(partial_filter)
            if isinstance(partial_filter, (list, tuple, set))
            else (partial_filter,)
        )
        expected_keys = tuple(keys)
        for index in collection.index_information().values():
            if tuple(index.get("key", [])) != expected_keys:
                continue
            if bool(index.get("unique")) != unique:
                continue
            if index.get("partialFilterExpression") in accepted_partials:
                return True
            if use_sparse_fallback and index.get("sparse"):
                return True
        return False

    # m0005 replaces the 0004 partial filters with $type-based ones (null
    # optional fields are omitted rather than stored as null).  verify accepts
    # both shapes: 0004's own run happens with the $exists form in place and
    # the post-0005 canonical state carries the $type form.
    if not has_index(
        db.project_member,
        [("project", 1), ("user", 1)],
        unique=True,
        partial_filter=(
            {"user": {"$exists": True}},
            {"user": {"$type": "objectId"}},
        ),
    ):
        return False
    if not has_index(
        db.project_member,
        [("external_id", 1)]
        if db.project_member.__class__.__module__.startswith("mongomock")
        else [("project", 1), ("external_id", 1)],
        unique=True,
        partial_filter=(
            {"external_id": {"$exists": True}},
            {"external_id": {"$type": "string"}},
        ),
    ):
        return False
    if not has_index(db.project_member, [("ik", 1)], unique=True):
        return False
    if not has_index(db.team_member, [("team", 1), ("user", 1)], unique=True):
        return False
    if db[REPORT_COLLECTION].count_documents({"_id": REPORT_SUMMARY_ID}) != 1:
        return False
    summary = (
        db[REPORT_COLLECTION].find_one({"_id": REPORT_SUMMARY_ID}).get("summary", {})
    )
    if summary.get("owner_issue_count", 0):
        return False
    return True
