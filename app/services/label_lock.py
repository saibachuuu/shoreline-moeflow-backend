"""Project-wide label isolation shared by HTTP and background model writes.

Locks are deliberately not TTL-deleted: a stalled writer must never be allowed
to continue behind a new owner. Failed destructive operations retain a recovery
marker and can only be unlocked by restoring their durable protection snapshot.
"""

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from functools import wraps
import time
from uuid import uuid4

from mongoengine import Document, IntField, QuerySet
from pymongo.errors import DuplicateKeyError

from app.core.api import APIError

_held = ContextVar("label_project_locks", default=frozenset())
_job_revisions = ContextVar("label_job_revisions", default={})


def capture_label_revision(identifier, *, file=False):
    from bson import ObjectId
    from app.models.project import Project

    pid = ObjectId(identifier)
    if file:
        from app.models.file import File

        row = File._get_collection().find_one({"_id": pid}, {"p": 1})
        pid = (row or {}).get("p")
    row = Project._get_collection().find_one({"_id": pid}, {"label_revision": 1})
    return (row or {}).get("label_revision", 0)


def label_job_task(kind):
    """Fence queued jobs, including jobs that load fresh rows after a restore."""

    def decorate(method):
        @wraps(method)
        def execute(*args, label_revision=None, **kwargs):
            from bson import ObjectId
            from app import celery
            from app.models import connect_db

            connect_db(celery.conf.app_config)
            identifier = args[1] if kind == "ocr" else args[0]
            pid = ObjectId(identifier)
            if kind == "file":
                from app.models.file import File

                row = File._get_collection().find_one({"_id": pid}, {"p": 1})
                pid = (row or {}).get("p")
            expected = 0 if label_revision is None else label_revision
            token = _job_revisions.set({**_job_revisions.get(), pid: expected})
            try:
                with label_lock(pid):
                    pass
                return method(*args, **kwargs)
            except LabelBusyError:
                return "跳过：项目标签版本已改变或正在批量操作，请重新发起任务"
            finally:
                _job_revisions.reset(token)

        return execute

    return decorate


class LabelBusyError(APIError):
    status_code = 409
    message = "项目标签正在操作或等待恢复，请刷新后重试"


def project_id_for(document):
    from app.models.project import Project

    if isinstance(document, Project):
        return document.pk
    if "project" in document._fields:
        return getattr(document.project, "pk", None)
    if "file" in document._fields:
        return project_id_for(document.file) if document.file else None
    if "source" in document._fields:
        return project_id_for(document.source) if document.source else None
    return None


@contextmanager
def label_lock(project_id, *, recovery=False):
    from mongoengine.connection import get_db
    from app.models.project import Project

    expected = _job_revisions.get().get(project_id)
    if expected is not None:
        row = Project._get_collection().find_one(
            {"_id": project_id}, {"label_revision": 1}
        )
        if (row or {}).get("label_revision", 0) != expected:
            raise LabelBusyError("后台任务的标签版本已过期")
    if project_id is None or project_id in _held.get():
        yield
        return
    locks = get_db()["project_label_lock"]
    owner = uuid4().hex
    import datetime

    deadline = time.monotonic() + 2
    while True:
        try:
            locks.insert_one(
                {
                    "_id": project_id,
                    "owner": owner,
                    "created": datetime.datetime.utcnow(),
                }
            )
            break
        except DuplicateKeyError as error:
            # Ordinary parallel GETs serialize briefly instead of spuriously
            # failing; long-running bulk operations still return a bounded 409.
            if time.monotonic() >= deadline:
                raise LabelBusyError() from error
            time.sleep(0.025)
    token = _held.set(_held.get() | {project_id})
    try:
        project = Project._get_collection().find_one({"_id": project_id})
        if (
            expected is not None
            and (project or {}).get("label_revision", 0) != expected
        ):
            raise LabelBusyError("后台任务的标签版本已过期")
        if project and project.get("label_recovery") and not recovery:
            raise LabelBusyError()
        yield
    finally:
        _held.reset(token)
        locks.delete_one({"_id": project_id, "owner": owner})


def _query_projects(query):
    """Resolve ownership without materializing model references recursively."""
    document = query._document
    from app.models.file import File, Source
    from app.services.project_statistics import ref_id

    if "project" in document._fields:
        values = query.distinct("project")
        return {ref_id(value) for value in values}
    if "file" in document._fields:
        file_ids = [ref_id(value) for value in query.distinct("file")]
    elif "source" in document._fields:
        source_ids = [ref_id(value) for value in query.distinct("source")]
        file_ids = [
            ref_id(row["f"])
            for row in Source._get_collection().find(
                {"_id": {"$in": source_ids}}, {"f": 1}
            )
        ]
    else:
        return set()
    return {
        ref_id(row["p"])
        for row in File._get_collection().find({"_id": {"$in": file_ids}}, {"p": 1})
    }


class LabelQuerySet(QuerySet):
    @contextmanager
    def _write_lock(self):
        with ExitStack() as stack:
            for pid in sorted(_query_projects(self), key=str):
                stack.enter_context(label_lock(pid))
            yield

    def update(self, *args, **kwargs):
        with self._write_lock():
            return super().update(*args, **kwargs)

    def modify(self, *args, **kwargs):
        with self._write_lock():
            return super().modify(*args, **kwargs)

    def delete(self, *args, **kwargs):
        with self._write_lock():
            return super().delete(*args, **kwargs)

    def insert(self, documents, *args, **kwargs):
        rows = list(documents) if isinstance(documents, (list, tuple)) else [documents]
        with ExitStack() as stack:
            for pid in sorted({project_id_for(row) for row in rows} - {None}, key=str):
                stack.enter_context(label_lock(pid))
            from app.models.project import Project

            for row in rows:
                row._assert_current()
                project = Project._get_collection().find_one(
                    {"_id": project_id_for(row)}, {"label_revision": 1}
                )
                row.label_epoch = (project or {}).get("label_revision", 0)
            return super().insert(documents, *args, **kwargs)


class LabelDocument(Document):
    meta = {"abstract": True, "queryset_class": LabelQuerySet}
    label_epoch = IntField(default=0)

    @classmethod
    def _from_son(cls, son, _auto_dereference=True, created=False):
        document = super()._from_son(
            son, _auto_dereference=_auto_dereference, created=created
        )
        # Stored with the row, not looked up after deserialization: a restore
        # racing a background read must not certify an old row as current.
        document._label_loaded_revision = son.get("label_epoch", 0)
        return document

    def _assert_current(self):
        from app.models.project import Project

        pid = project_id_for(self)
        row = (
            Project._get_collection().find_one({"_id": pid}, {"label_revision": 1})
            if pid
            else None
        )
        current = (row or {}).get("label_revision", 0)
        if getattr(self, "_label_loaded_revision", current) != current:
            raise LabelBusyError("标签数据已被批量修改，旧任务或旧对象不能继续写入")
        # New records created from old background-task references are stale too.
        for name in ("project", "file", "source", "target"):
            reference = self._data.get(name)
            if isinstance(reference, Project) and reference.label_revision != current:
                raise LabelBusyError("项目数据已改变，请重新加载后写入")
            if (
                isinstance(reference, LabelDocument)
                and getattr(reference, "_label_loaded_revision", current) != current
            ):
                raise LabelBusyError("标签引用已过期，请重新加载后写入")

    def save(self, *args, **kwargs):
        from app.models.project import Project

        with label_lock(project_id_for(self)):
            self._assert_current()
            row = Project._get_collection().find_one(
                {"_id": project_id_for(self)}, {"label_revision": 1}
            )
            self.label_epoch = (row or {}).get("label_revision", 0)
            result = super().save(*args, **kwargs)
            self._label_loaded_revision = self.label_epoch
            return result

    def reload(self, *args, **kwargs):
        result = super().reload(*args, **kwargs)
        self._label_loaded_revision = self.label_epoch
        return result

    def update(self, *args, **kwargs):
        with label_lock(project_id_for(self)):
            self._assert_current()
            return super().update(*args, **kwargs)

    def modify(self, *args, **kwargs):
        with label_lock(project_id_for(self)):
            self._assert_current()
            return super().modify(*args, **kwargs)

    def delete(self, *args, **kwargs):
        with label_lock(project_id_for(self)):
            self._assert_current()
            return super().delete(*args, **kwargs)


def protect_methods(cls, names):
    """Keep multi-write model operations inside one re-entrant lock."""
    for name in names:
        original = getattr(cls, name)

        def wrap(method):
            @wraps(method)
            def locked(self, *args, **kwargs):
                with label_lock(project_id_for(self)):
                    self._assert_current()
                    return method(self, *args, **kwargs)

            return locked

        setattr(cls, name, wrap(original))


def install_label_request_guard(app):
    from bson import ObjectId
    from flask import g, request
    from app.models.file import File, Source, Translation, Tip
    from app.models.project import Project
    from app.models.target import Target

    @app.before_request
    def guard():
        args = request.view_args or {}
        pid = None
        try:
            if args.get("project_id"):
                pid = ObjectId(args["project_id"])
            else:
                for key, cls in (
                    ("file_id", File),
                    ("source_id", Source),
                    ("translation_id", Translation),
                    ("tip_id", Tip),
                    ("target_id", Target),
                ):
                    if args.get(key):
                        obj = cls.objects(id=args[key]).first()
                        if obj:
                            pid = project_id_for(obj)
                        break
        except (ValueError, TypeError):
            return None
        if pid is None:
            return None
        g.label_project_id = pid
        row = Project._get_collection().find_one(
            {"_id": pid}, {"label_revision": 1, "label_recovery": 1}
        )
        if row is None:
            return None
        g.label_revision = row.get("label_revision", 0)
        is_operation = "/label-" in request.path or "/snapshots" in request.path
        if row.get("label_recovery") and not is_operation:
            return LabelBusyError().to_tuple()
        if request.method != "OPTIONS":
            try:
                context = label_lock(pid, recovery=is_operation)
                context.__enter__()
                g.label_lock_context = context
                # Re-read after lock acquisition: the pre-lock value can race a
                # completed restore. Reads also hold the lock until serialization.
                row = Project._get_collection().find_one(
                    {"_id": pid}, {"label_revision": 1}
                )
                g.label_revision = (row or {}).get("label_revision", 0)
                expected = request.headers.get("X-Label-Revision")
                label_path = any(
                    f"/{resource}" in request.path
                    for resource in (
                        "files",
                        "sources",
                        "translations",
                        "tips",
                        "targets",
                        "label-",
                        "snapshots",
                    )
                )
                if (
                    request.method not in ("GET", "HEAD")
                    and label_path
                    and g.label_revision > 0
                    and expected is None
                ):
                    return LabelBusyError(
                        "批量操作后的标签写入必须携带版本号，请重新打开编辑页面"
                    ).to_tuple()
                if (
                    request.method not in ("GET", "HEAD")
                    and expected is not None
                    and expected != str(g.label_revision)
                ):
                    return LabelBusyError(
                        "标签数据已被批量修改，请重新打开编辑页面"
                    ).to_tuple()
            except LabelBusyError as error:
                return error.to_tuple()

    @app.after_request
    def revision_headers(response):
        pid = getattr(g, "label_project_id", None)
        if pid is not None:
            row = Project._get_collection().find_one(
                {"_id": pid}, {"label_revision": 1}
            )
            response.headers["X-Label-Project"] = str(pid)
            response.headers["X-Label-Revision"] = str(
                (row or {}).get("label_revision", 0)
            )
            response.headers.add(
                "Access-Control-Expose-Headers", "X-Label-Project, X-Label-Revision"
            )
        return response

    @app.teardown_request
    def release(error):
        context = getattr(g, "label_lock_context", None)
        if context is not None:
            del g.label_lock_context
            context.__exit__(None, None, None)
