import datetime

from app.core.views import MoeAPIView
from app.decorators.auth import token_required
from app.decorators.url import fetch_model
from app.exceptions import NoPermissionError, TipEmptyError
from app.models.file import Source, Tip
from app.models.project import ProjectPermission
from app.validators.tip import CreateTipSchema, EditTipSchema


class SourceTipListAPI(MoeAPIView):
    @token_required
    @fetch_model(Source)
    def post(self, source: Source):
        """
        @api {post} /v1/sources/<source_id>/tips 新增或更新原文批注
        @apiVersion 1.0.0
        @apiName add_source_tip
        @apiGroup Tip
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam {String} content 批注内容，不能为空
        @apiParam {String} target_id 目标语言ID，批注按目标语言隔离

        @apiDescription 同一校对者对同一原文、同一目标语言只保留一条批注，
        重复提交会覆盖原有内容。

        @apiSuccessExample {json} 返回示例
        {

        }
        """
        if not self.current_user.can(
            source.file.project, ProjectPermission.PROOFREAD_TRA
        ):
            raise NoPermissionError
        data = self.get_json(CreateTipSchema())
        target = source.file.project.target_by_id(data["target_id"])
        tip = source.create_tip(data["content"], target=target, user=self.current_user)
        source.update_cache("edit_time", datetime.datetime.utcnow())
        try:
            from app.models.presence import ProjectPresence

            ProjectPresence.heartbeat(
                source.file.project, self.current_user, action="editing_text"
            )
        except Exception:
            pass
        return tip.to_api()


class TipAPI(MoeAPIView):
    @token_required
    @fetch_model(Tip)
    def put(self, tip: Tip):
        """
        @api {put} /v1/tips/<tip_id> 修改批注
        @apiVersion 1.0.0
        @apiName edit_tip
        @apiGroup Tip
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam {String} content 批注内容，不能为空

        @apiSuccessExample {json} 返回示例
        {

        }
        """
        if tip.user != self.current_user:
            raise NoPermissionError
        if not self.current_user.can(
            tip.source.file.project, ProjectPermission.PROOFREAD_TRA
        ):
            raise NoPermissionError
        data = self.get_json(EditTipSchema())
        if data["content"].strip() == "":
            raise TipEmptyError
        tip.content = data["content"]
        tip.edit_time = datetime.datetime.utcnow()
        tip.save()
        tip.source.update_cache("edit_time", datetime.datetime.utcnow())
        try:
            from app.models.presence import ProjectPresence

            ProjectPresence.heartbeat(
                tip.source.file.project, self.current_user, action="editing_text"
            )
        except Exception:
            pass
        return tip.to_api()

    @token_required
    @fetch_model(Tip)
    def delete(self, tip: Tip):
        """
        @api {delete} /v1/tips/<tip_id> 删除批注
        @apiVersion 1.0.0
        @apiName delete_tip
        @apiGroup Tip
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiSuccessExample {json} 返回示例
        204
        """
        if tip.user != self.current_user:
            raise NoPermissionError
        if not self.current_user.can(
            tip.source.file.project, ProjectPermission.PROOFREAD_TRA
        ):
            raise NoPermissionError
        source = tip.source
        tip.delete()
        source.update_cache("edit_time", datetime.datetime.utcnow())
