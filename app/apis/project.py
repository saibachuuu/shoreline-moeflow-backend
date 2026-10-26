import datetime

from flask import current_app
from flask_babel import gettext

from app.constants.file import FileType, ParseStatus
from app.constants.output import OutputTypes
from app.constants.project import ProjectStatus
from app.constants.storage import requires_local_thumbnail
from app.core.responses import MoePagination
from app.core.views import MoeAPIView
from app.decorators.auth import token_required
from app.decorators.url import fetch_model
from app.exceptions import (
    NoPermissionError,
    RequestDataEmptyError,
)
from app.exceptions.output import OutputTooFastError
from app.exceptions.project import ProjectFinishedError, TargetNotExistError
from app.models.output import Output
from app.models.project import Project, ProjectPermission
from app.models.target import Target
from app.models.team import TeamPermission
from app.services.identity_permission import IdentityPermissionService
from app.services.project_member import ProjectMemberService
from app.services.proofread_image import marked_image_url
from app.tasks.email import send_email
from app.tasks.ocr import ocr
from app.tasks.output_project import output_project
from app.tasks.proofread_draft import bake_and_dispatch
from app.validators.project import (
    CreateOutputSchema,
    CreateProjectTargetSchema,
    EditProjectSchema,
)


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
        # 职位锁定为团队创建者专属设置
        if (
            "position_lock_enabled" in data or "position_lock_tags" in data
        ) and not IdentityPermissionService.is_team_creator(
            self.current_user, project.team
        ):
            raise NoPermissionError(gettext("只有团队创建者可以修改职位锁定设置"))
        if data.get("position_lock_enabled") is False:
            # 总开关关闭时二级锁定一并清空
            data["position_lock_tags"] = []
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
        if not requires_local_thumbnail(current_app.config):
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
        from app.models.file import File, Source, Tip, Translation
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

        if current_app.config.get("ENABLE_NOTIFICATIONS"):
            import hashlib

            from flask import request

            from app.models.notification import Notification
            from app.services.notification_content import fail

            operation_key = request.headers.get("Idempotency-Key")
            operation = {
                "target_id": str(target.id),
                "file_id": str(file_id or ""),
                "cc_myself": cc_myself,
            }
            if operation_key:
                digest = hashlib.sha256(
                    f"{self.current_user.id}:core:{operation_key}".encode()
                ).hexdigest()
                old = Notification.objects(key=digest).first()
                if old:
                    if (
                        old.event_type != "proofread_feedback"
                        or old.project_id != project.id
                        or old.payload.get("operation") != operation
                    ):
                        fail("相同操作键不能用于不同内容", 409)
                    return {
                        "message": gettext(
                            "校对反馈已受理，邮件投递状态请查看通知记录"
                        ),
                        "notification_id": str(old.id),
                        "state": old.state,
                    }, 202

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
        total_tips = 0
        for file in scan_files:
            sources = Source.objects(file=file).order_by("rank")
            # 一次取出本页全部批注，按原文分组挂到对应标签上
            tips_by_source = {}
            for tip in Tip.objects(source__in=sources, target=target).order_by(
                "create_time"
            ):
                tips_by_source.setdefault(str(tip.source.id), []).append(
                    {
                        "author": tip.user.name if tip.user else None,
                        "content": tip.content,
                        "edit_time": tip.edit_time.strftime("%Y-%m-%d %H:%M"),
                    }
                )
            page_tip_count = sum(len(v) for v in tips_by_source.values())
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
                        "tips": tips_by_source.get(str(source.id), []),
                        "x": source.x or 0,
                        "y": source.y or 0,
                        "position_type": source.position_type,
                    }
                )

            if changed_count > 0 or page_tip_count > 0:
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
                        "tip_count": page_tip_count,
                        "changed_label_nums": [
                            lbl["label_num"] for lbl in page_labels if lbl["is_changed"]
                        ],
                        "labels": page_labels,
                    }
                )
                total_tips += page_tip_count

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

        if not to_emails and not current_app.config.get("ENABLE_NOTIFICATIONS"):
            if cc_myself and current_user_email:
                to_emails = [current_user_email]
            else:
                raise NoTranslatorMemberError

        target_lang_name = target.language.lo_name if target.language else ""
        site_name = current_app.config.get("SITE_NAME", "萌翻")
        site_origin = str(current_app.config.get("SITE_ORIGIN", "")).rstrip("/")
        project_url = f"{site_origin}/project/{project.id}" if site_origin else ""
        total_changed = sum(p["changed_count"] for p in changed_pages)
        subject = gettext("[%s] 校对稿修改反馈 - %s") % (
            project.name,
            target_lang_name,
        )

        if current_app.config.get("ENABLE_NOTIFICATIONS"):
            import uuid

            from app.models.notification import NotificationReceipt
            from app.services import notifications as notification_service
            from app.services.proofread_image import DEFAULT_OPACITY

            # 寄送瞬间快照团队标记设置，已受理的邮件不受后续设置变更影响
            team = project.team
            marker_enabled = bool(getattr(team, "proofread_marker_enabled", True))
            marker_opacity = getattr(team, "proofread_marker_opacity", None)
            if marker_opacity is None:
                marker_opacity = DEFAULT_OPACITY
            pending = {
                "site_name": site_name,
                "project_name": project.name,
                "target_language_name": target_lang_name,
                "sender_name": self.current_user.name,
                "sender_email": current_user_email,
                "send_time": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                "total_changed_labels": total_changed,
                "total_tips": total_tips,
                "changed_pages": changed_pages,
                "project_url": project_url,
                "reply_address": current_user_email,
                "marker_enabled": marker_enabled,
                "marker_opacity": marker_opacity,
            }
            payload = {
                "pending": pending,
                "reply_to": current_user_email,
                "cc_myself": cc_myself,
                "operation": operation,
            }
            notification_data = {
                "category": "project",
                "scope_id": str(project.id),
                "title": "「%s」校对稿 · 正在生成标注图（0/%d）"
                % (project.name, len(changed_pages)),
                "body": gettext(
                    "%(sender)s 寄送了 %(language)s 校对反馈，共 %(pages)s 页、%(changes)s 处修改。\n[project]%(project_id)s[/project]",
                    sender=self.current_user.name,
                    language=target_lang_name,
                    pages=len(changed_pages),
                    changes=total_changed,
                    project_id=project.id,
                ),
                "audience": {"mode": "condition", "tags": ["translator"]},
                "email": True,
                # 草稿态不会被 notification_scan 自动派发；烤图完成后由后台任务翻转为 preparing
                "draft": True,
            }
            candidate = notification_service.make_note(
                self.current_user,
                notification_data,
                trusted=True,
                event_type="proofread_feedback",
                payload=payload,
            )
            if not notification_service.recipient_ids(candidate):
                raise NoTranslatorMemberError
            note = notification_service.publish(
                self.current_user,
                notification_data,
                operation_key or str(uuid.uuid4()),
                trusted=True,
                event_type="proofread_feedback",
                payload=payload,
            )
            # 受理即让发件人在收件箱看到进行中的任务（receipt 平时在派发时才创建）
            NotificationReceipt.objects(
                notification_id=note.id, user_id=self.current_user.id
            ).update_one(
                upsert=True,
                set_on_insert__created_at=datetime.datetime.utcnow(),
                set_on_insert__archived=False,
            )
            bake_and_dispatch.delay(str(note.id))
            return {
                "message": gettext(
                    "校对反馈已受理，正在后台生成标注图，完成后自动寄出"
                ),
                "notification_id": str(note.id),
                "state": note.state,
                "changed_pages_count": len(changed_pages),
                "changed_labels_count": total_changed,
            }, 202

        # 直发路径没有后台任务，仍同步生成烤图（保持既有行为）
        from app.services.proofread_image import DEFAULT_OPACITY

        team = project.team
        marker_enabled = bool(getattr(team, "proofread_marker_enabled", True))
        marker_opacity = getattr(team, "proofread_marker_opacity", None)
        if marker_opacity is None:
            marker_opacity = DEFAULT_OPACITY
        for page in changed_pages:
            page["marked_image_url"] = marked_image_url(
                File.objects(id=page["file_id"]).first(),
                page["labels"],
                enabled=marker_enabled,
                opacity=marker_opacity,
            )

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
                "total_tips": total_tips,
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
