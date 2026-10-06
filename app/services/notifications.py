"""Core notification authorization and persistence; never imports optional modules."""

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from bson import ObjectId
from flask import current_app
from mongoengine import NotUniqueError
from app.models.notification import (
    Notification,
    NotificationReceipt,
    NotificationDelivery,
    NotificationPolicy,
    NotificationPreference,
    NotificationAudit,
    NotificationVerifiedEmail,
    NotificationThrottle,
)
from app.models.user import User
from app.models.team import Team
from app.models.project import Project
from app.models.team_member import TeamMember
from app.models.project_member import ProjectMember
from app.models.identity_tag import WORKER_TAGS, TEAM_BASE_TAGS
from app.services.notification_content import (
    fail,
    prepare,
    plain,
    normalize,
    search_tokens,
    visible_nodes,
)

CATEGORIES = ("system", "team", "project", "personal")
SOURCES = {}


def utcnow():
    return datetime.utcnow()


def enabled():
    if not current_app.config.get("ENABLE_NOTIFICATIONS", False):
        fail("通知系统尚未启用", 503)


def oid(value):
    if not isinstance(value, (str, ObjectId)) or not ObjectId.is_valid(value):
        fail("无效的对象标识符")
    return ObjectId(value)


def active(user):
    return user is not None and not user.banned


def administrator(user):
    return active(user) and user.admin is True


def require_admin(user):
    if not administrator(user):
        fail("需要有效站点管理员权限", 403)


def scope(category, scope_id):
    model = Team if category == "team" else Project if category == "project" else None
    if model is None:
        return None
    group = model.objects(id=oid(scope_id)).first()
    if not group:
        fail("关联对象不可访问", 404)
    return group


def can_publish(user, category, scope_id=None):
    if not active(user):
        return False
    if category == "system":
        return administrator(user)
    if category not in ("team", "project"):
        return False
    group = scope(category, scope_id)
    if category == "team":
        member = TeamMember.objects(team=group, user=user, status="active").first()
        if not member:
            return False
        if member.base_tag == "creator":
            return True
        policy = NotificationPolicy.objects(team_id=group.id).first()
        return bool(member.base_tag == "admin" and policy and policy.team_admin)
    member = ProjectMember.objects(project=group, user=user, status="active").first()
    if not member or group.status == 1:
        return False
    if group.owner_user:
        if group.owner_user.id == user.id:
            return True
    elif "creator" in (member.tags or []):
        return True
    policy = NotificationPolicy.objects(team_id=group.team.id).first()
    return bool("admin" in (member.tags or []) and policy and policy.project_admin)


def author_allowed(note):
    user = User.objects(id=note.actor_id).first()
    if not active(user):
        return False
    if note.event_type == "proofread_feedback":
        project = Project.objects(id=note.project_id).first()
        return bool(project and user.can(project, "project:PROOFREAD_TRA"))
    if note.category == "personal":
        return note.source in SOURCES
    try:
        return can_publish(user, note.category, note.project_id or note.team_id)
    except Exception:
        return False


def audit(user_id, action, note=None, detail=None, key=None):
    key = key or str(uuid.uuid4())
    NotificationAudit.objects(key=key).update_one(
        upsert=True,
        set_on_insert__actor_id=user_id,
        set_on_insert__action=action,
        set_on_insert__notification_id=note.id if note else None,
        set_on_insert__detail=detail or {},
        set_on_insert__created_at=utcnow(),
    )


def throttle(user, action, limit):
    """Fixed-minute CAS counter: concurrent requests cannot all pass a count/read check."""
    now = utcnow()
    bucket = now.strftime("%Y%m%d%H%M")
    key = f"{user.id}:{action}:{bucket}"
    try:
        NotificationThrottle.objects(key=key, count__lt=limit).modify(
            upsert=True,
            new=True,
            inc__count=1,
            set_on_insert__expires_at=now + timedelta(minutes=2),
        )
    except NotUniqueError:
        fail("操作过于频繁，请稍后重试", 429)


def date_value(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        fail("日期必须是带时区的 ISO 8601 格式")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        fail("日期必须是带时区的 ISO 8601 格式")


def serialize_date(value):
    return value.isoformat() + "Z" if value else None


def validate_audience(category, value):
    if not isinstance(value, dict) or set(value) - {
        "mode",
        "user_ids",
        "base_roles",
        "worker_qualifications",
        "tags",
        "site_roles",
        "team_ids",
        "project_ids",
    }:
        fail("收件人筛选无效")
    result = dict(value)
    mode = result.get("mode")
    if mode not in ("all", "condition", "manual"):
        fail("请明确选择收件人模式")
    for key in set(result) - {"mode"}:
        items = result[key]
        if (
            not isinstance(items, list)
            or len(items) > (1000 if key == "user_ids" else 30)
            or any(not isinstance(v, str) for v in items)
        ):
            fail("收件人条件必须是有界字符串列表")
        result[key] = sorted(set(items))
    if mode == "all" and set(result) != {"mode"}:
        fail("全体模式不能附带其他条件")
    if mode == "manual":
        if set(result) != {"mode", "user_ids"} or not result["user_ids"]:
            fail("请选择接收用户")
        result["user_ids"] = [str(oid(v)) for v in result["user_ids"]]
    elif "user_ids" in result:
        fail("手选用户与条件模式互斥")
    if mode == "condition" and not any(v for k, v in result.items() if k != "mode"):
        fail("请选择至少一项筛选条件")
    if category == "system":
        if set(result) & {"base_roles", "worker_qualifications", "tags"}:
            fail("站点通知不支持该成员条件")
        if set(result.get("site_roles", [])) - {"admin", "member"}:
            fail("站点身份条件无效")
        if result.get("team_ids") and result.get("project_ids"):
            fail("团队范围与项目范围不能混用")
        for key in ("team_ids", "project_ids"):
            if key in result:
                result[key] = [str(oid(v)) for v in result[key]]
                model = Team if key == "team_ids" else Project
                if model.objects(id__in=result[key]).count() != len(result[key]):
                    fail("接收范围不存在")
    elif category in ("team", "project"):
        if set(result) & {"site_roles", "team_ids", "project_ids"}:
            fail("不能扩大当前通知范围")
        if set(result.get("base_roles", [])) - set(TEAM_BASE_TAGS):
            fail("成员身份无效")
        if set(result.get("worker_qualifications", []) + result.get("tags", [])) - set(
            WORKER_TAGS
        ):
            fail("工作人员职位无效")
    elif mode != "manual":
        fail("个人通知只能由受信服务指定用户")
    return result


def recipient_ids(note):
    """Bounded lookup, no user dereference per membership and no raw email lists."""
    a = note.audience
    query = User.objects(banned__ne=True)
    ids = None
    if note.category in ("team", "project"):
        if note.category == "team":
            if not Team.objects(id=note.team_id).first():
                return []
            members = TeamMember.objects(team=note.team_id, status="active")
            if a.get("base_roles"):
                members = members.filter(base_tag__in=a["base_roles"])
            if a.get("worker_qualifications") or a.get("tags"):
                members = members.filter(
                    worker_qualifications__in=a.get("worker_qualifications")
                    or a["tags"]
                )
        else:
            if not Project.objects(id=note.project_id).first():
                return []
            members = ProjectMember.objects(
                project=note.project_id, status="active", user__ne=None
            )
            roles = a.get("base_roles", [])
            if roles:
                from mongoengine.queryset.visitor import Q

                selected = Q(tags__in=[r for r in roles if r != "member"])
                if "member" in roles:
                    selected |= Q(tags__nin=["creator", "admin"])
                members = members.filter(selected)
            if a.get("tags") or a.get("worker_qualifications"):
                members = members.filter(
                    tags__in=a.get("tags") or a["worker_qualifications"]
                )
        limit = current_app.config.get("NOTIFICATION_MAX_AUDIENCE", 20000)
        rows = list(members.only("user").as_pymongo().limit(limit + 1))
        if len(rows) > limit:
            fail("接收范围超过配置上限")
        ids = {r["user"] for r in rows if r.get("user")}
        if note.event_type == "proofread_feedback" and note.payload.get("cc_myself"):
            # Copy is an explicit existing operation, not a broadcast widening.
            actor = User.objects(id=note.actor_id).first()
            project = Project.objects(id=note.project_id).first()
            if (
                active(actor)
                and project
                and actor.can(project, "project:PROOFREAD_TRA")
            ):
                ids.add(actor.id)
    if a.get("team_ids") or a.get("project_ids"):
        model = TeamMember if a.get("team_ids") else ProjectMember
        args = (
            {"team__in": a["team_ids"]}
            if a.get("team_ids")
            else {"project__in": a["project_ids"]}
        )
        limit = current_app.config.get("NOTIFICATION_MAX_AUDIENCE", 20000)
        rows = list(
            model.objects(status="active", **args)
            .only("user")
            .as_pymongo()
            .limit(limit + 1)
        )
        if len(rows) > limit:
            fail("接收范围超过配置上限")
        ids = {r["user"] for r in rows if r.get("user")}
    if a.get("site_roles") and len(a["site_roles"]) == 1:
        query = (
            query.filter(admin=True)
            if a["site_roles"][0] == "admin"
            else query.filter(admin__ne=True)
        )
    if a["mode"] == "manual":
        selected = {oid(v) for v in a["user_ids"]}
        ids = selected if ids is None else selected & ids
    if ids is not None:
        query = query.filter(id__in=ids)
    limit = current_app.config.get("NOTIFICATION_MAX_AUDIENCE", 20000)
    result = [u.id for u in query.only("id").order_by("id").limit(limit + 1)]
    if len(result) > limit:
        fail("接收范围超过配置上限")
    return result


def recipient_eligible(note, user):
    """Recheck one recipient without materializing the entire audience for each email."""
    if not active(user):
        return False
    audience = note.audience
    if audience.get("mode") == "manual" and str(user.id) not in audience.get(
        "user_ids", []
    ):
        return False
    site_roles = audience.get("site_roles", [])
    if site_roles and ("admin" if user.admin else "member") not in site_roles:
        return False
    if (
        audience.get("team_ids")
        and not TeamMember.objects(
            team__in=audience["team_ids"], user=user, status="active"
        ).first()
    ):
        return False
    if (
        audience.get("project_ids")
        and not ProjectMember.objects(
            project__in=audience["project_ids"], user=user, status="active"
        ).first()
    ):
        return False
    if note.category in ("system", "personal"):
        return True
    positions = set(audience.get("tags") or audience.get("worker_qualifications") or [])
    wanted = set(audience.get("base_roles", []))
    if note.category == "team":
        if not Team.objects(id=note.team_id).only("id").first():
            return False
        member = TeamMember.objects(
            team=note.team_id, user=user, status="active"
        ).first()
        return bool(
            member
            and (not wanted or member.base_tag in wanted)
            and (not positions or positions.intersection(member.worker_qualifications))
        )
    project = Project.objects(id=note.project_id).first()
    if not project:
        return False
    if (
        note.event_type == "proofread_feedback"
        and note.payload.get("cc_myself")
        and user.id == note.actor_id
    ):
        return user.can(project, "project:PROOFREAD_TRA")
    member = ProjectMember.objects(project=project, user=user, status="active").first()
    if not member:
        return False
    tags = set(member.tags or [])
    base = tags.intersection({"creator", "admin"}) or {"member"}
    return (not wanted or bool(wanted.intersection(base))) and (
        not positions or bool(positions.intersection(tags))
    )


def email_reason(user, category):
    if not user.email:
        return "no_email"
    if not current_app.config.get("ENABLE_USER_EMAIL"):
        return "channel_disabled"
    pref = NotificationPreference.objects(user_id=user.id).first()
    if pref and not pref.categories.get(category, True):
        return "preference_disabled"
    digest = hashlib.sha256(user.email.strip().lower().encode()).hexdigest()
    if (
        not current_app.config.get("NOTIFICATION_TRUST_EXISTING_EMAILS", False)
        and not NotificationVerifiedEmail.objects(digest=digest).first()
    ):
        return "unverified_email"
    return ""


def mark_verified(email):
    digest = hashlib.sha256(email.strip().lower().encode()).hexdigest()
    NotificationVerifiedEmail.objects(digest=digest).update_one(
        upsert=True, set__verified_at=utcnow()
    )


def make_note(
    user, data, *, event_type="announcement", payload=None, source="core", trusted=False
):
    enabled()
    if not active(user):
        fail("用户不可用", 403)
    if not isinstance(data, dict) or set(data) - {
        "category",
        "scope_id",
        "title",
        "body",
        "audience",
        "email",
        "publish_at",
        "expires_at",
        "draft",
    }:
        fail("通知请求包含不支持的字段")
    category = data.get("category")
    if category not in CATEGORIES or (category == "personal" and not trusted):
        fail("不能发布此类通知", 403)
    scope_id = data.get("scope_id")
    group = scope(category, scope_id)
    if not trusted and not can_publish(user, category, scope_id):
        fail("没有当前范围的通知发送权限", 403)
    title, body = data.get("title"), data.get("body", "")
    if (
        not isinstance(title, str)
        or not 1 <= len(title.strip()) <= 200
        or any(ord(c) < 32 for c in title)
    ):
        fail("标题须为 1–200 字符的单行文本")
    for field in ("email", "draft"):
        if field in data and type(data[field]) is not bool:
            fail("通知开关必须为布尔值")
    audience = validate_audience(category, data.get("audience"))
    nodes = prepare(body, user)
    publish_at = date_value(data.get("publish_at")) or utcnow()
    expires_at = date_value(data.get("expires_at"))
    if expires_at and expires_at <= max(utcnow(), publish_at):
        fail("失效时间必须晚于发送时间")
    if publish_at > utcnow() + timedelta(days=366):
        fail("定时时间不能超过一年")
    text = normalize(title + " " + plain(nodes))
    note = Notification(
        category=category,
        source=source,
        event_type=event_type,
        actor_id=user.id,
        team_id=group.id
        if category == "team"
        else (group.team.id if category == "project" else None),
        project_id=group.id if category == "project" else None,
        title=title.strip(),
        body=body,
        nodes=nodes,
        search_text=text,
        search_tokens=search_tokens(text),
        audience=audience,
        requested_email=data.get("email", False),
        payload=payload or {},
        publish_at=publish_at,
        expires_at=expires_at,
        state="draft"
        if data.get("draft")
        else ("scheduled" if publish_at > utcnow() else "preparing"),
    )
    return note


def preview(user, data, *, candidate_query="", **kwargs):
    note = make_note(user, data, **kwargs)
    throttle(user, "preview", 60)
    ids = recipient_ids(note)
    if note.audience["mode"] == "manual" and len(ids) != len(note.audience["user_ids"]):
        fail("指定用户不在有效接收范围内")
    users = User.objects(id__in=ids).only("id", "name", "email", "banned")
    summary = {}
    for u in users:
        reason = (
            email_reason(u, note.category) if note.requested_email else "not_requested"
        )
        summary[reason or "eligible"] = summary.get(reason or "eligible", 0) + 1
    if candidate_query:
        q = re.escape(candidate_query[:100])
        users = users.filter(
            __raw__={
                "$or": [
                    {"n": {"$regex": q, "$options": "i"}},
                    {"as": {"$regex": q, "$options": "i"}},
                ]
            }
        )
    return {
        "count": len(ids),
        "email_count": summary.get("eligible", 0),
        "email_summary": summary,
        "users": [{"id": str(u.id), "name": u.name} for u in users.limit(100)],
        "nodes": visible_nodes(note.nodes, user),
    }


def publish(user, data, key, **kwargs):
    note = make_note(user, data, **kwargs)
    if not isinstance(key, str) or not 8 <= len(key) <= 160:
        fail("需要 8–160 字符的幂等操作键")
    note.key = hashlib.sha256(f"{user.id}:{note.source}:{key}".encode()).hexdigest()
    fingerprint_data = {
        "data": data,
        "type": note.event_type,
        "payload": kwargs.get("payload", {}),
    }
    note.fingerprint = hashlib.sha256(
        json.dumps(fingerprint_data, sort_keys=True, default=str).encode()
    ).hexdigest()
    old = Notification.objects(key=note.key).first()
    if old:
        if old.fingerprint != note.fingerprint:
            fail("相同操作键不能用于不同内容", 409)
        return old
    ids = recipient_ids(note)
    if not ids:
        fail("没有符合条件的接收人")
    if note.audience["mode"] == "manual" and len(ids) != len(note.audience["user_ids"]):
        fail("指定用户不在有效接收范围内")
    throttle(user, "publish", 20)
    try:
        note.save(force_insert=True)
    except NotUniqueError:
        old = Notification.objects(key=note.key).first()
        if old.fingerprint != note.fingerprint:
            fail("相同操作键不能用于不同内容", 409)
        return old
    audit(user.id, "publish", note, key=note.key + ":publish")
    note.update(set__publish_audited=True)
    return note


def live(note):
    return (
        not note.revoked_at
        and note.state not in ("draft", "scheduled", "preparing", "cancelled")
        and (not note.expires_at or note.expires_at > utcnow())
    )


def receipt_visible(note, user, cache=None):
    if not live(note) or not active(user):
        return False
    # Scope-membership/qualification changes apply to historic receipts too.
    cache = cache if cache is not None else {}
    key = (
        note.category,
        str(note.team_id),
        str(note.project_id),
        json.dumps(note.audience, sort_keys=True),
        str(note.actor_id) if note.event_type == "proofread_feedback" else "",
        note.payload.get("cc_myself", False),
    )
    if key not in cache:
        cache[key] = recipient_eligible(note, user)
    return cache[key]


def get_note(value):
    note = Notification.objects(id=oid(value)).first()
    if not note:
        fail("通知不存在或不可访问", 404)
    return note


def get_receipt(user, value):
    note = get_note(value)
    receipt = NotificationReceipt.objects(
        notification_id=note.id, user_id=user.id
    ).first()
    if not receipt or not receipt_visible(note, user):
        fail("通知不存在或不可访问", 404)
    return note, receipt


def scope_owner(user, category, scope_id):
    if category == "team":
        return bool(
            TeamMember.objects(
                team=oid(scope_id), user=user, status="active", base_tag="creator"
            ).first()
        )
    if category == "project":
        group = scope(category, scope_id)
        if group.owner_user:
            return group.owner_user.id == user.id
        return bool(
            ProjectMember.objects(
                project=group, user=user, status="active", tags="creator"
            ).first()
        )
    return False


def can_view_proofread(user, category, scope_id):
    if not active(user) or category != "project":
        return False
    project = scope(category, scope_id)
    return bool(user.can(project, "project:PROOFREAD_TRA"))


def ordinary_manage(user, note):
    # The migrated operation retains proofread permission; it never grants generic send rights.
    if (
        note.event_type == "proofread_feedback"
        and note.actor_id == user.id
        and can_view_proofread(user, note.category, note.project_id)
    ):
        return
    scope_id = note.project_id or note.team_id
    if not can_publish(user, note.category, scope_id):
        fail("没有当前范围的发件管理权限", 403)
    if note.actor_id != user.id and not scope_owner(user, note.category, scope_id):
        fail("只能管理本人发送或本人创建范围的通知", 403)


def detail(
    note,
    user,
    *,
    management=False,
    receipt=None,
    full=True,
    actor_names=None,
    delivery_counts=None,
    search_query="",
):
    if actor_names is None:
        actor = User.objects(id=note.actor_id).only("name").first()
        actor_name = actor.name if actor else "已删除用户"
    else:
        actor_name = actor_names.get(note.actor_id, "已删除用户")
    result = {
        "id": str(note.id),
        "category": note.category,
        "source": note.source,
        "event_type": note.event_type,
        "title": note.title,
        "actor_id": str(note.actor_id),
        "actor_name": actor_name,
        "team_id": str(note.team_id) if note.team_id else None,
        "project_id": str(note.project_id) if note.project_id else None,
        "state": note.state,
        "version": note.version,
        "created_at": serialize_date(note.created_at),
        "publish_at": serialize_date(note.publish_at),
        "expires_at": serialize_date(note.expires_at),
        "revoked_at": serialize_date(note.revoked_at),
        "recipient_count": note.recipient_count,
        "read": bool(receipt and receipt.read_at),
        "archived": bool(receipt and receipt.archived),
    }
    if search_query:
        text = note.search_text or ""
        start = max(0, text.find(normalize(search_query)) - 45)
        result["excerpt"] = (
            ("…" if start else "")
            + text[start : start + 180]
            + ("…" if len(text) > start + 180 else "")
        )
    if full:
        result["nodes"] = visible_nodes(note.nodes, user)
        if note.event_type == "proofread_feedback":
            result["feedback_text"] = note.payload.get("text", "")
    if management:
        result.update(
            body=note.body if full else None,
            audience=note.audience,
            email=note.requested_email,
            revoke_reason=note.revoke_reason,
            error=note.error,
        )
        if delivery_counts is None:
            states = {}
            for row in NotificationDelivery._get_collection().aggregate(
                [
                    {"$match": {"notification_id": note.id}},
                    {"$group": {"_id": "$state", "count": {"$sum": 1}}},
                ],
                maxTimeMS=2000,
            ):
                states[row["_id"]] = row["count"]
        else:
            states = delivery_counts.get(note.id, {})
        result["deliveries"] = states
        if full:
            result["delivery_reasons"] = {
                row["_id"]: row["count"]
                for row in NotificationDelivery._get_collection().aggregate(
                    [
                        {
                            "$match": {
                                "notification_id": note.id,
                                "reason": {"$nin": ["", None]},
                            }
                        },
                        {"$group": {"_id": "$reason", "count": {"$sum": 1}}},
                    ],
                    maxTimeMS=2000,
                )
            }
    return result


def detail_many(notes, user, *, management=False, receipts=None, search_query=""):
    actors = {
        u.id: u.name
        for u in User.objects(id__in={n.actor_id for n in notes}).only("name")
    }
    counts = {}
    if management and notes:
        for row in NotificationDelivery._get_collection().aggregate(
            [
                {"$match": {"notification_id": {"$in": [n.id for n in notes]}}},
                {
                    "$group": {
                        "_id": {"notification": "$notification_id", "state": "$state"},
                        "count": {"$sum": 1},
                    }
                },
            ],
            maxTimeMS=2000,
        ):
            counts.setdefault(row["_id"]["notification"], {})[row["_id"]["state"]] = (
                row["count"]
            )
    receipts = receipts or {}
    return [
        detail(
            n,
            user,
            management=management,
            receipt=receipts.get(n.id),
            full=False,
            actor_names=actors,
            delivery_counts=counts,
            search_query=search_query,
        )
        for n in notes
    ]


def revoke(user, note, data, key, admin=False):
    if admin:
        require_admin(user)
    else:
        ordinary_manage(user, note)
    if (
        not isinstance(data, dict)
        or set(data) - {"confirmed", "version", "reason"}
        or data.get("confirmed") is not True
    ):
        fail("必须二次确认撤回")
    reason = data.get("reason")
    if (
        not isinstance(reason, str)
        or not 1 <= len(reason.strip()) <= 500
        or type(data.get("version")) is not int
    ):
        fail("需要撤回理由及当前版本")
    if not key or not 8 <= len(key) <= 160:
        fail("需要撤回操作幂等键")
    operation = hashlib.sha256(f"{user.id}:{key}".encode()).hexdigest()
    if note.revoked_at:
        if note.moderation_key == operation:
            audit(
                user.id,
                note.moderation_action,
                note,
                {"reason": note.revoke_reason},
                key=operation + ":revoke",
            )
            note.update(set__moderation_audited=True)
            return note
        fail("通知已经撤回，请刷新", 409)
    updated = Notification.objects(
        id=note.id, version=data["version"], revoked_at=None
    ).modify(
        new=True,
        set__revoked_at=utcnow(),
        set__revoked_by=user.id,
        set__revoke_reason=reason.strip(),
        set__moderation_key=operation,
        set__moderation_action="admin_revoke" if admin else "revoke",
        set__moderation_audited=False,
        set__state="cancelled",
        inc__version=1,
    )
    if not updated:
        fail("通知版本已变化，请刷新后再次确认", 409)
    audit(
        user.id,
        "admin_revoke" if admin else "revoke",
        updated,
        {"reason": reason.strip()},
        key=operation + ":revoke",
    )
    updated.update(set__moderation_audited=True)
    NotificationDelivery.objects(
        notification_id=note.id, state__in=["pending", "retry_wait"]
    ).update(set__state="cancelled", set__reason="revoked")
    return updated


def edit_draft(user, note, data):
    ordinary_manage(user, note)
    if not isinstance(data, dict) or type(data.get("version")) is not int:
        fail("需要当前草稿版本")
    data = dict(data)
    version = data.pop("version")
    if note.revoked_at or note.state not in ("draft", "scheduled"):
        fail("只能编辑未发布且未撤回的草稿", 409)
    # Scope/author/type are immutable, including for an administrator.
    if data.get("category") != note.category or str(data.get("scope_id") or "") != str(
        note.project_id or note.team_id or ""
    ):
        fail("不能修改通知类别或范围")
    fresh = make_note(user, data)
    ids = recipient_ids(fresh)
    if not ids or (
        fresh.audience["mode"] == "manual"
        and len(ids) != len(fresh.audience["user_ids"])
    ):
        fail("草稿接收人为空或包含范围外用户")
    changes = {
        f"set__{f}": getattr(fresh, f)
        for f in (
            "title",
            "body",
            "nodes",
            "search_text",
            "search_tokens",
            "audience",
            "requested_email",
            "publish_at",
            "expires_at",
            "state",
        )
    }
    updated = Notification.objects(
        id=note.id,
        version=version,
        revoked_at=None,
        state__in=["draft", "scheduled"],
        lease_until__lte=utcnow(),
    ).modify(new=True, inc__version=1, **changes)
    if not updated:
        fail("草稿版本冲突", 409)
    audit(user.id, "edit_draft", updated)
    return updated


def register_source(name, recipient_validator):
    if (
        not re.fullmatch(r"module:[a-z][a-z0-9_]{0,63}", name)
        or name in SOURCES
        or not callable(recipient_validator)
    ):
        raise ValueError("invalid notification source registration")
    SOURCES[name] = recipient_validator


def publish_personal(actor, source, data, key):
    # This is an internal Python contract, not a public HTTP endpoint.
    if (
        source not in SOURCES
        or data.get("category") != "personal"
        or not SOURCES[source](actor, data)
    ):
        fail("未获授权的通知来源或接收关系", 403)
    return publish(
        actor, data, key, trusted=True, source=source, event_type=source + ".event"
    )
