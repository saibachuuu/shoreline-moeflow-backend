"""Stable errors for the identity-tag runtime APIs."""

from flask_babel import lazy_gettext

from .base import MoeError


class IdentityError(MoeError):
    code = 5199
    identity_code = "IDENTITY_ERROR"


class InvalidIdentityRequestError(IdentityError):
    code = 5100
    identity_code = "INVALID_REQUEST"
    message = lazy_gettext("身份标签请求参数错误")


class InvalidIdentityTagError(IdentityError):
    code = 5101
    status_code = 422
    identity_code = "INVALID_IDENTITY_TAG"
    message = lazy_gettext("项目身份标签无效或不可分配")


class TeamQualificationRequiredError(IdentityError):
    code = 5102
    status_code = 422
    identity_code = "TEAM_QUALIFICATION_REQUIRED"
    message = lazy_gettext("目标用户没有对应的团队工作人员资格")


class IdentityMemberNotFoundError(IdentityError):
    code = 5103
    status_code = 404
    identity_code = "MEMBER_NOT_FOUND"
    message = lazy_gettext("项目成员不存在")


class IdentityTeamMemberNotFoundError(IdentityError):
    code = 5104
    status_code = 404
    identity_code = "TEAM_MEMBER_NOT_FOUND"
    message = lazy_gettext("团队成员不存在")


class IdentityUserNotFoundError(IdentityError):
    code = 5105
    status_code = 404
    identity_code = "USER_NOT_FOUND"
    message = lazy_gettext("用户不存在")


class MemberAlreadyExistsError(IdentityError):
    code = 5106
    status_code = 409
    identity_code = "MEMBER_ALREADY_EXISTS"
    message = lazy_gettext("成员已存在")


class MemberCapacityReachedError(IdentityError):
    code = 5107
    status_code = 409
    identity_code = "MEMBER_CAPACITY_REACHED"
    message = lazy_gettext("成员数量已达到上限")


class ProjectMemberVersionConflictError(IdentityError):
    code = 5108
    status_code = 409
    identity_code = "PROJECT_MEMBER_VERSION_CONFLICT"
    message = lazy_gettext("项目成员版本已变化，请刷新后重试")


class MemberMergeRequiredError(IdentityError):
    code = 5109
    status_code = 409
    identity_code = "MEMBER_MERGE_REQUIRED"
    message = lazy_gettext("项目中已存在该用户成员，需要显式合并")


class OwnerConflictError(IdentityError):
    code = 5110
    status_code = 409
    identity_code = "OWNER_CONFLICT"
    message = lazy_gettext("项目 owner 条件已变化")


class MemberAlreadyOwnerError(IdentityError):
    code = 5111
    status_code = 409
    identity_code = "MEMBER_ALREADY_OWNER"
    message = lazy_gettext("项目 owner 不能被移除或降权")


class ProjectStateConflictError(IdentityError):
    code = 5112
    status_code = 409
    identity_code = "PROJECT_STATE_CONFLICT"
    message = lazy_gettext("项目状态不允许执行该操作")


class ClearOperationRetryableError(IdentityError):
    code = 5113
    status_code = 503
    identity_code = "CLEAR_OPERATION_RETRYABLE"
    message = lazy_gettext("项目内容清理未完成，请重试")


class AliasValidationError(IdentityError):
    code = 5114
    status_code = 422
    identity_code = "ALIAS_INVALID"
    message = lazy_gettext("alias 格式或数量无效")


class IdentityVersionConflictError(IdentityError):
    code = 5115
    status_code = 409
    identity_code = "VERSION_CONFLICT"
    message = lazy_gettext("版本已变化，请刷新后重试")


class ProtectedIdentityTagError(IdentityError):
    code = 5116
    status_code = 422
    identity_code = "PROTECTED_IDENTITY_TAG"
    message = lazy_gettext("系统身份标签受保护")


class IdentityPolicyInUseError(IdentityError):
    code = 5117
    status_code = 409
    identity_code = "IDENTITY_POLICY_IN_USE"
    message = lazy_gettext("仍有成员使用该标签，不能删除")


class IdentityOperationInProgressError(IdentityError):
    code = 5118
    status_code = 409
    identity_code = "IDENTITY_OPERATION_IN_PROGRESS"
    message = lazy_gettext("同一成员操作正在处理中，请稍后重试")
