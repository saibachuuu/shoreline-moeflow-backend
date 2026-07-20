import json
import re
from typing import Any

from flask_babel import gettext

from app.core.views import MoeAPIView
from app.decorators.auth import token_required
from app.decorators.url import fetch_model
from app.exceptions import NoPermissionError
from app.models.file import File, Source, Translation
from app.models.project import Project, ProjectPermission
from app.models.target import Target


WORKER_ROLES_CN = ("图源", "扫图", "修图", "翻译", "校对", "嵌字")
WORKER_ROLES_EN = (
    "provider",
    "scan",
    "scan_retoucher",
    "translator",
    "proofreader",
    "picture_editor",
)
WORKER_ROLE_CN_TO_EN = dict(zip(WORKER_ROLES_CN, WORKER_ROLES_EN))
WORKER_ROLE_EN_TO_CN = dict(zip(WORKER_ROLES_EN, WORKER_ROLES_CN))

WORKER_ROLES = WORKER_ROLES_EN
WORKER_LINE_PATTERN = re.compile(
    r"^(图源|扫图|修图|翻译|翻校|校对|嵌字|provider|scan|scan_retoucher|translator|proofreader|picture_editor)[:：](.+)$"
)
Workers = dict[str, list[str]]


def normalize_workers(value: Any) -> Workers:
    if not isinstance(value, dict):
        return {}

    normalized: Workers = {}
    for role_en in WORKER_ROLES_EN:
        role_cn = WORKER_ROLE_EN_TO_CN.get(role_en)
        names = value.get(role_en) or value.get(role_cn)
        if not isinstance(names, list):
            continue
        unique_names = list(
            dict.fromkeys(
                name.strip() for name in names if isinstance(name, str) and name.strip()
            )
        )
        if unique_names:
            normalized[role_en] = unique_names
    return normalized


def load_workers(project: Project) -> Workers:
    try:
        return normalize_workers(json.loads(project.workers or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}


def save_workers(project: Project, workers: Workers) -> Workers:
    normalized = normalize_workers(workers)
    project.update(workers=json.dumps(normalized, ensure_ascii=False))
    return normalized


class ProjectWorkersAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    def get(self, project: Project):
        if not self.current_user.can(project, ProjectPermission.ACCESS):
            raise NoPermissionError
        return {"workers": load_workers(project)}

    @token_required
    @fetch_model(Project)
    def put(self, project: Project):
        if not self.current_user.can(project, ProjectPermission.CHANGE):
            raise NoPermissionError
        workers = save_workers(project, self.get_json().get("workers", {}))
        return {"message": gettext("修改成功"), "workers": workers}


class ProjectWorkersParseAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    def post(self, project: Project):
        if not self.current_user.can(project, ProjectPermission.CHANGE):
            raise NoPermissionError
        workers = save_workers(project, parse_workers_from_project(project))
        return {"message": gettext("解析完成"), "workers": workers}


class ProjectWorkersAddAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    def post(self, project: Project):
        if not self.current_user.can(project, ProjectPermission.CHANGE):
            raise NoPermissionError

        workers = load_workers(project)
        additions = normalize_workers(self.get_json().get("workers", {}))
        for role, names in additions.items():
            workers[role] = list(dict.fromkeys([*workers.get(role, []), *names]))
        workers = save_workers(project, workers)
        return {"message": gettext("添加成功"), "workers": workers}


def extract_workers_from_text(text: str) -> Workers:
    workers: Workers = {}
    for line in text.splitlines():
        match = WORKER_LINE_PATTERN.match(line.strip())
        if not match:
            continue

        role, name = match.groups()
        if role == "翻校":
            resolved_roles = ("translator", "proofreader")
        elif role in WORKER_ROLE_CN_TO_EN:
            resolved_roles = (WORKER_ROLE_CN_TO_EN[role],)
        elif role in WORKER_ROLES_EN:
            resolved_roles = (role,)
        else:
            continue
        for resolved_role in resolved_roles:
            workers.setdefault(resolved_role, [])
            if name.strip() not in workers[resolved_role]:
                workers[resolved_role].append(name.strip())
    return workers


def parse_workers_from_project(project: Project) -> Workers:
    target = Target.objects(project=project).first()
    if not target:
        return {}

    workers: Workers = {}
    for file in File.objects(project=project, activated=True):
        for source in Source.objects(file=file).order_by("rank"):
            translation = Translation.objects(
                source=source, target=target, selected=True
            ).first()
            if not translation:
                translation = (
                    Translation.objects(source=source, target=target)
                    .order_by("-edit_time")
                    .first()
                )
            if not translation:
                continue

            content = translation.proofread_content or translation.content
            for role, names in extract_workers_from_text(content or "").items():
                workers[role] = list(dict.fromkeys([*workers.get(role, []), *names]))
    return workers
