import json
import re
from flask_babel import gettext
from app.core.views import MoeAPIView
from app.decorators.auth import token_required
from app.decorators.url import fetch_model
from app.exceptions import NoPermissionError
from app.models.project import Project, ProjectPermission
from app.models.file import File, Source, Translation
from app.models.target import Target


class ProjectWorkersAPI(MoeAPIView):

    @token_required
    @fetch_model(Project)
    def get(self, project: Project):
        if not self.current_user.can(project, ProjectPermission.ACCESS):
            raise NoPermissionError
        try:
            workers = json.loads(project.workers) if project.workers else {}
        except json.JSONDecodeError:
            workers = {}
        return {"workers": workers}

    @token_required
    @fetch_model(Project)
    def put(self, project: Project):
        if not self.current_user.can(project, ProjectPermission.ACCESS):
            raise NoPermissionError
        data = self.get_json()
        workers = data.get("workers", {})
        project.update(workers=json.dumps(workers))
        return {"message": gettext("修改成功"), "workers": workers}


class ProjectWorkersParseAPI(MoeAPIView):

    @token_required
    @fetch_model(Project)
    def post(self, project: Project):
        if not self.current_user.can(project, ProjectPermission.ACCESS):
            raise NoPermissionError
        translations = parse_translations_from_project(project)

        extracted_workers = {}
        for translation in translations:
            first_line = translation.split('\n')[0].strip()
            if re.match(r'^(图源|扫图|翻译|翻校|校对|嵌字)[:：]', first_line):
                workers_in_text = extract_workers_from_text(translation)
                for role, names in workers_in_text.items():
                    if role not in extracted_workers:
                        extracted_workers[role] = []
                    for name in names:
                        if name not in extracted_workers[role]:
                            extracted_workers[role].append(name)

        project.update(workers=json.dumps(extracted_workers))

        return {
            "message": gettext("解析完成"),
            "workers": extracted_workers
        }


def extract_workers_from_text(text: str) -> dict:
    workers = {}
    pattern = r'^(图源|扫图|翻译|翻校|校对|嵌字)[:：](.+)$'

    for line in text.split('\n'):
        line = line.strip()
        match = re.match(pattern, line)
        if match:
            role = match.group(1)
            name = match.group(2).strip()
            if name:
                if role == "翻校":
                    roles = ["翻译", "校对"]
                else:
                    roles = [role]

                for r in roles:
                    if r not in workers:
                        workers[r] = []
                    if name not in workers[r]:
                        workers[r].append(name)

    return workers


class ProjectWorkersAddAPI(MoeAPIView):

    @token_required
    @fetch_model(Project)
    def post(self, project: Project):
        if not self.current_user.can(project, ProjectPermission.ACCESS):
            raise NoPermissionError
        data = self.get_json()
        new_workers = data.get("workers", {})

        try:
            current_workers = json.loads(project.workers) if project.workers else {}
        except json.JSONDecodeError:
            current_workers = {}

        for role, names in new_workers.items():
            if role not in current_workers:
                current_workers[role] = []
            for name in names:
                if name not in current_workers[role]:
                    current_workers[role].append(name)

        project.update(workers=json.dumps(current_workers))
        return {"message": gettext("添加成功"), "workers": current_workers}


def parse_translations_from_project(project: Project) -> list:
    target = Target.objects(project=project).first()
    if not target:
        return []

    files = File.objects(project=project, activated=True)

    result = []

    for file in files:
        sources = Source.objects(file=file).order_by("rank")

        for source in sources:
            trans = Translation.objects(
                source=source,
                target=target,
                selected=True
            ).first()

            if not trans:
                trans = Translation.objects(
                    source=source,
                    target=target
                ).order_by("-edit_time").first()

            if trans:
                content = trans.proofread_content if trans.proofread_content else trans.content
                if content:
                    result.append(content)

    return result
