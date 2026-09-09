from app.constants.file import FileType, ParseStatus
from app.constants.output import OutputTypes
from app.tasks.ocr import ocr
from app.models.team import TeamPermission
from app.tasks.email import send_email
import datetime
from app.exceptions.project import ProjectFinishedError, TargetNotExistError
from flask import current_app
from flask_babel import gettext

from app.core.views import MoeAPIView
from app.decorators.auth import token_required
from app.decorators.url import fetch_model
from app.exceptions import (
    NoPermissionError,
    RequestDataEmptyError,
)
from app.models.project import Project, ProjectPermission
from app.models.target import Target
from app.models.output import Output
from app.services.project_member import ProjectMemberService
from app.constants.project import ProjectStatus
from app.constants.storage import StorageType
from app.validators.project import (
    EditProjectSchema,
    CreateProjectTargetSchema,
    CreateOutputSchema,
)
from app.core.responses import MoePagination
from app.tasks.output_project import output_project
from app.exceptions.output import OutputTooFastError


class ProjectAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    def get(self, project: Project):
        """
        @api {get} /v1/projects/<project_id> 获取项目信息
        @apiVersion 1.0.0
        @apiName getProjectAPI
        @apiGroup Project
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiSuccessExample {json} 返回示例
        {

        }
        """
        # 检查用户权限
        if not self.current_user.can(
            project, ProjectPermission.ACCESS
        ) and not self.current_user.can(project.team, TeamPermission.ACCESS):
            raise NoPermissionError(gettext("您没有此项目的访问权限"))
        data = project.to_api(user=self.current_user)
        data["member_summary"] = ProjectMemberService.member_summaries([project]).get(
            str(project.id), []
        )
        return data

    @token_required
    @fetch_model(Project)
    def put(self, project: Project):
        """
        @api {put} /v1/projects/<project_id> 修改项目
        @apiVersion 1.0.0
        @apiName putProjectAPI
        @apiGroup Project
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam {String} project_id 项目id
        @apiParamExample {json} 请求示例
        {
            "name":"123123"
        }

        @apiSuccess {String} msg 提示消息
        @apiSuccessExample {json} 返回示例
        {
            "message": "修改成功"
        }

        @apiUse ValidateError
        """
        # 检查项目是否已完成
        if project.status != ProjectStatus.WORKING:
            raise ProjectFinishedError
        # 检查是否有访问权限
        if not self.current_user.can(project, ProjectPermission.CHANGE):
            raise NoPermissionError
        data = self.get_json(EditProjectSchema(), context={"project": project})
        if not data:
            raise RequestDataEmptyError
        project.update(**data)
        project.reload()
        project_data = project.to_api(user=self.current_user)
        project_data["member_summary"] = ProjectMemberService.member_summaries(
            [project]
        ).get(str(project.id), [])
        return {
            "message": gettext("修改成功"),
            "project": project_data,
        }


class ProjectTargetListAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    def get(self, project: Project):
        """
        @api {get} /v1/projects/<project_id>/targets 获取翻译目标列表
        @apiVersion 1.0.0
        @apiName getProjectTargetListAPI
        @apiGroup Project
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiSuccessExample {json} 返回示例
        {

        }
        """
        if not self.current_user.can(project, ProjectPermission.ACCESS):
            raise NoPermissionError
        # query = self.get_query({}, TargetSearchSchema())
        # TODO: 支持通过名字筛选语言，不过因为查询的名字是i18n的，所以比如用日文搜索English就搜不到
        p = MoePagination(max_limit=0)
        objects = project.targets().skip(p.skip).limit(p.limit)
        return p.set_objects(objects)

    @token_required
    @fetch_model(Project)
    def post(self, project: Project):
        """
        @api {get} /v1/projects/<project_id>/targets 新增翻译目标
        @apiVersion 1.0.0
        @apiName postProjectTargetListAPI
        @apiGroup Project
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiSuccessExample {json} 返回示例
        {

        }
        """
        if not self.current_user.can(project, ProjectPermission.ADD_TARGET):
            raise NoPermissionError
        data = self.get_json(CreateProjectTargetSchema())
        target = Target.create(project=project, language=data["language"])
        return {"message": gettext("添加目标语言成功"), "target": target.to_api()}


class ProjectOutputListAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    def post(self, project: Project):
        if not self.current_user.can(project, ProjectPermission.OUTPUT_TRA):
            raise NoPermissionError
        outputs_json = []
        for target in project.targets():
            # 等待一定时间后允许再次导出
            last_output = target.outputs().first()
            if last_output and (
                datetime.datetime.utcnow() - last_output.create_time
                < datetime.timedelta(
                    seconds=current_app.config.get("OUTPUT_WAIT_SECONDS", 60 * 5)
                )
            ):
                continue
            # 删除三个导出之前的
            old_targets = target.outputs().skip(2)
            Output.delete_real_files(old_targets)
            old_targets.delete()
            # 创建新target
            output = Output.create(
                project=project,
                target=target,
                user=self.current_user,
                type=OutputTypes.ALL,
            )
            output_project(str(output.id))
            outputs_json.append(output.to_api())
        return outputs_json


class ProjectTargetOutputListAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    @fetch_model(Target)
    def get(self, project: Project, target: Target):
        """
        @api {get} /v1/projects/<project_id>/targets/<target_id>/outputs 获取项目导出
        @apiVersion 1.0.0
        @apiName getProjectLabelplusAPI
        @apiGroup Project
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam {string} target 翻译目标

        @apiSuccessExample {json} 返回示例
        {

        }
        """
        if not self.current_user.can(project, ProjectPermission.OUTPUT_TRA):
            raise NoPermissionError
        if target.project != project:
            raise TargetNotExistError
        return [output.to_api() for output in target.outputs()]

    @token_required
    @fetch_model(Project)
    @fetch_model(Target)
    def post(self, project: Project, target: Target):
        """
        @api {post} /v1/projects/<project_id>/targets/<target_id>/outputs 新增项目导出
        @apiVersion 1.0.0
        @apiName postProjectLabelplusAPI
        @apiGroup Project
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam {string} target 翻译目标

        @apiSuccessExample {json} 返回示例
        {

        }
        """
        if not self.current_user.can(project, ProjectPermission.OUTPUT_TRA):
            raise NoPermissionError
        if target.project != project:
            raise TargetNotExistError
        # 等待一定时间后允许再次导出
        last_output = target.outputs().first()
        if last_output and (
            datetime.datetime.utcnow() - last_output.create_time
            < datetime.timedelta(
                seconds=current_app.config.get("OUTPUT_WAIT_SECONDS", 60 * 5)
            )
        ):
            raise OutputTooFastError
        data = self.get_json(CreateOutputSchema())
        # 删除三个导出之前的
        old_targets = target.outputs().skip(2)
        Output.delete_real_files(old_targets)
        old_targets.delete()
        # 创建新target
        output = Output.create(
            project=project,
            target=target,
            user=self.current_user,
            type=data["type"],
            file_ids_include=data["file_ids_include"],
            file_ids_exclude=data["file_ids_exclude"],
        )
        output_project(str(output.id))
        return output.to_api()


# 暂未启用此接口
class ProjectLabelplusAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    @fetch_model(Target)
    def get(self, project: Project, target: Target):
        """
        @api {get} /v1/projects/<project_id>/targets/<target_id>/labelplus
            获取labelplus翻译内容
        @apiVersion 1.0.0
        @apiName getProjectLabelplusAPI
        @apiGroup Project
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam {string} target 翻译目标

        @apiSuccessExample {json} 返回示例
        {

        }
        """
        if not self.current_user.can(project, ProjectPermission.OUTPUT_TRA):
            raise NoPermissionError
        if target.project != project:
            raise TargetNotExistError
        return project.to_labelplus(target=target)


class ProjectOCRAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    def post(self, project: Project):
        """
        @api {post} /v1/projects/<project_id>/ocr 为项目 OCR
        @apiVersion 1.0.0
        @apiName postProjectOCRAPI
        @apiGroup Project
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam {Project} project 项目

        @apiSuccessExample {json} 返回示例
        {

        }
        """
        # 检查用户权限
        if not self.current_user.can(project.team, TeamPermission.USE_OCR_QUOTA):
            raise NoPermissionError(
                gettext("您没有此项目所在团队使用自动标记限额的权限")
            )
        if not project.source_language.g_ocr_code:
            raise NoPermissionError(gettext("源语言不支持自动标记"))
        if project.ocring:
            raise NoPermissionError(gettext("自动标记进行中"))
        images = project.files(type_only=FileType.IMAGE).filter(
            parse_status__in=[ParseStatus.NOT_START, ParseStatus.PARSE_FAILED]
        )
        if images.count() == 0:
            raise NoPermissionError(gettext("未发现需要标记的图片"))
        if project.team.ocr_quota_left < images.count():
            raise NoPermissionError(gettext("团队限额不足"))
        images.update(parse_status=ParseStatus.QUEUING)
        ocr("project", str(project.id))
        return {"message": gettext("已开始自动标记")}


class ProjectThumbnailAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    def post(self, project: Project):
        """
        @api {post} /v1/projects/<project_id>/thumbnails 批量重新生成缩略图和采样图
        @apiVersion 1.0.0
        @apiName postProjectThumbnailsAPI
        @apiGroup Project
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiSuccessExample {json} 返回示例
        {
            "message": "已为 50 张图片触发缩略图生成任务",
            "count": 50
        }
        """
        if not self.current_user.can(project, ProjectPermission.CHANGE):
            raise NoPermissionError(gettext("您没有此项目的访问权限"))
        if not (
            current_app.config["STORAGE_TYPE"] == StorageType.LOCAL_STORAGE
            or (
                current_app.config["STORAGE_TYPE"] == StorageType.OSS
                and current_app.config.get("OSS_BUCKET_STYLE") == "R2"
            )
        ):
            return {
                "message": gettext("当前存储模式无需重建缩略图"),
                "count": 0,
            }

        from app.models.file import File
        from app.tasks.thumbnail import create_thumbnail

        images = File.objects(
            project=project,
            type=FileType.IMAGE,
            activated=True,
        )
        count = 0
        for image in images:
            create_thumbnail(str(image.id))
            count += 1

        return {
            "message": gettext("已为 %(count)s 张图片触发缩略图生成任务", count=count),
            "count": count,
        }


def generate_diff_html(orig: str, proof: str) -> str:
    """生成带行内高亮对比的 HTML 差异文本"""
    import difflib
    import html

    matcher = difflib.SequenceMatcher(None, orig or "", proof or "")
    result = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            result.append(html.escape(orig[i1:i2]))
        elif tag == "delete":
            deleted = html.escape(orig[i1:i2])
            result.append(
                f'<del style="background-color: #ffdce0; color: #af1b28; text-decoration: line-through; padding: 1px 3px; border-radius: 2px; margin: 0 1px;">{deleted}</del>'
            )
        elif tag == "insert":
            inserted = html.escape(proof[j1:j2])
            result.append(
                f'<ins style="background-color: #dcffe4; color: #116329; text-decoration: none; padding: 1px 3px; border-radius: 2px; font-weight: bold; margin: 0 1px;">{inserted}</ins>'
            )
        elif tag == "replace":
            deleted = html.escape(orig[i1:i2])
            inserted = html.escape(proof[j1:j2])
            result.append(
                f'<del style="background-color: #ffdce0; color: #af1b28; text-decoration: line-through; padding: 1px 3px; border-radius: 2px; margin: 0 1px;">{deleted}</del>'
                f'<ins style="background-color: #dcffe4; color: #116329; text-decoration: none; padding: 1px 3px; border-radius: 2px; font-weight: bold; margin: 0 1px;">{inserted}</ins>'
            )
    return "".join(result)


class ProjectSendProofreadDraftAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    @fetch_model(Target)
    def post(self, project: Project, target: Target):
        """
        @api {post} /v1/projects/<project_id>/targets/<target_id>/send-proofread-draft 向翻译寄送校对稿
        @apiVersion 1.0.0
        @apiName postProjectSendProofreadDraft
        @apiGroup Project
        @apiUse APIHeader
        @apiUse TokenHeader
        @apiParam {Boolean} [cc_myself=true] 是否为我自己抄送一份
        @apiParam {String} [file_id] 可选，限制为单个文件
        """
        import html
        from app.exceptions.project import (
            FileNotExistError,
            NoTranslatorMemberError,
            ProofreadDraftNoChangesError,
        )
        from app.models.file import File, Source, Translation
        from app.models.project_member import ProjectMember
        from app.validators.project import SendProofreadDraftSchema

        # 检查校对权限
        if not self.current_user.can(project, ProjectPermission.PROOFREAD_TRA):
            raise NoPermissionError(gettext("您没有校对权限，无法寄送校对稿"))
        if target.project != project:
            raise TargetNotExistError

        data = self.get_json(SendProofreadDraftSchema())
        cc_myself = data.get("cc_myself", True)
        file_id = data.get("file_id")

        # 获取全部图片用于计算全局页码
        all_image_files = list(
            File.objects(project=project, type=FileType.IMAGE, activated=True).order_by(
                "dir_sort_name", "sort_name"
            )
        )
        file_page_map = {f.id: i + 1 for i, f in enumerate(all_image_files)}

        if file_id:
            target_file = File.objects(
                id=file_id, project=project, type=FileType.IMAGE, activated=True
            ).first()
            if not target_file:
                raise FileNotExistError
            scan_files = [target_file]
        else:
            scan_files = all_image_files

        changed_pages = []
        for file in scan_files:
            sources = Source.objects(file=file).order_by("rank")
            page_labels = []
            changed_count = 0
            for idx, source in enumerate(sources):
                label_num = source.rank + 1
                translation = Translation.objects(
                    source=source, target=target, selected=True
                ).first() or source.best_translation(target=target)
                orig_content = (translation.content if translation else "") or ""
                proof_content = (
                    translation.proofread_content if translation else ""
                ) or ""

                is_changed = False
                if (
                    proof_content.strip()
                    and proof_content.strip() != orig_content.strip()
                ):
                    is_changed = True
                    changed_count += 1
                    diff_html = generate_diff_html(orig_content, proof_content)
                else:
                    diff_html = html.escape(proof_content or orig_content or "")

                page_labels.append(
                    {
                        "source_id": str(source.id),
                        "label_num": label_num,
                        "source_text": source.content or "",
                        "orig_translation": orig_content,
                        "proofread_translation": proof_content,
                        "is_changed": is_changed,
                        "diff_html": diff_html,
                    }
                )

            if changed_count > 0:
                img_url = ""
                try:
                    resample = getattr(file, "resample_url", "")
                    if resample and resample != "generating":
                        img_url = resample
                    else:
                        img_url = getattr(file, "url", "") or ""
                except Exception:
                    img_url = getattr(file, "url", "") or ""

                changed_pages.append(
                    {
                        "page_number": file_page_map.get(file.id, 1),
                        "file_id": str(file.id),
                        "file_name": file.name,
                        "image_url": img_url,
                        "total_sources": len(sources),
                        "changed_count": changed_count,
                        "changed_label_nums": [
                            lbl["label_num"] for lbl in page_labels if lbl["is_changed"]
                        ],
                        "labels": page_labels,
                    }
                )

        if not changed_pages:
            raise ProofreadDraftNoChangesError

        # 查询项目中“翻译”栏活跃成员的邮箱
        translators = ProjectMember.objects(
            project=project,
            status="active",
            tags="translator",
            user__exists=True,
        )
        translator_emails = []
        for m in translators:
            if m.user and m.user.email:
                addr = m.user.email.strip().lower()
                if addr and addr not in translator_emails:
                    translator_emails.append(addr)

        current_user_email = (
            self.current_user.email.strip().lower()
            if (self.current_user and self.current_user.email)
            else None
        )

        to_emails = list(translator_emails)
        cc_emails = []
        if cc_myself and current_user_email:
            if current_user_email in to_emails:
                pass
            else:
                cc_emails.append(current_user_email)

        if not to_emails:
            if cc_myself and current_user_email:
                to_emails = [current_user_email]
            else:
                raise NoTranslatorMemberError

        target_lang_name = target.language.lo_name if target.language else ""
        site_name = current_app.config.get("SITE_NAME", "萌翻")
        site_origin = str(current_app.config.get("SITE_ORIGIN", "")).rstrip("/")
        project_url = f"{site_origin}/project/{project.id}" if site_origin else ""
        total_changed = sum(p["changed_count"] for p in changed_pages)
        subject = f"[{project.name}] 校对稿修改反馈 - {target_lang_name}"

        send_email(
            to_address=to_emails,
            subject=subject,
            template="email/proofread_draft",
            template_data={
                "site_name": site_name,
                "project": project,
                "target": target,
                "target_language_name": target_lang_name,
                "sender_name": self.current_user.name,
                "sender_email": current_user_email,
                "send_time": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                "total_changed_labels": total_changed,
                "changed_pages": changed_pages,
                "project_url": project_url,
                "reply_address": current_user_email,
            },
            reply_address=current_user_email,
            from_username=f"{site_name} - 校对反馈",
            cc_address=cc_emails if cc_emails else None,
        )

        return {
            "message": gettext("校对稿已成功发送至翻译邮箱"),
            "recipients": to_emails + cc_emails,
            "changed_pages_count": len(changed_pages),
            "changed_labels_count": total_changed,
        }
