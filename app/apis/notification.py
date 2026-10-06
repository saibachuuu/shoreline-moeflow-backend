"""Notification HTTP surfaces: inbox, scoped authoring, and isolated site moderation."""

import re
import hashlib
import json
from flask import Blueprint, request
from mongoengine import NotUniqueError
from app.core.views import MoeAPIView
from app.decorators.auth import token_required
from app.models.notification import (
    Notification,
    NotificationReceipt,
    NotificationPolicy,
    NotificationPreference,
    NotificationDelivery,
)
from app.models.team_member import TeamMember
from app.models.user import User
from app.services import notifications as svc
from app.services.notification_content import (
    fail,
    search_tokens,
    normalize,
    visible_nodes,
    NotificationError,
)


def body():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        fail("请求必须是 JSON 对象")
    return value


def key():
    return request.headers.get("Idempotency-Key", "")


def page_size():
    try:
        value = int(request.args.get("limit", 20))
        if not 1 <= value <= 100:
            raise ValueError()
        return value
    except ValueError:
        fail("分页数量必须在 1–100 之间")


def apply_search(query):
    q = request.args.get("q", "").strip()
    if q:
        if len(q) > 100:
            fail("搜索词过长")
        if len(q) == 24 and re.fullmatch("[a-fA-F0-9]+", q):
            return query.filter(id=svc.oid(q))
        if len(normalize(q)) < 2:
            fail("搜索词至少两个字符")
        value = normalize(q)
        query = query.filter(
            search_tokens__all=search_tokens(value)[:30], search_text__contains=value
        )
    category = request.args.get("category")
    if category:
        if category not in svc.CATEGORIES:
            fail("通知分类无效")
        query = query.filter(category=category)
    return query.max_time_ms(2000)


def receipt_page(user, *, unread_only=False, filtered=True):
    receipts = NotificationReceipt.objects(user_id=user.id)
    if unread_only or request.args.get("unread") == "true":
        receipts = receipts.filter(read_at=None, archived=False)
    else:
        receipts = receipts.filter(archived=request.args.get("archived") == "true")
    cursor = request.args.get("cursor") if filtered else None
    if cursor:
        receipts = receipts.filter(id__lt=svc.oid(cursor))
    cache = {}
    # Bounded batches of roots; do not load payloads from other users' notifications.
    after = None
    while True:
        batch_query = receipts.filter(id__lt=after) if after else receipts
        batch = list(batch_query.order_by("-id").limit(100))
        if not batch:
            return
        notes = {
            n.id: n
            for n in (apply_search if filtered else lambda q: q)(
                Notification.objects(id__in=[r.notification_id for r in batch])
            ).exclude(
                "nodes",
                "body",
                "search_tokens",
                "payload.html",
                "payload.text",
            )
        }
        for receipt in batch:
            note = notes.get(receipt.notification_id)
            if note and svc.receipt_visible(note, user, cache):
                yield note, receipt
        after = batch[-1].id


class NotificationCapabilitiesAPI(MoeAPIView):
    @token_required
    def get(self):
        return capabilities(
            self.current_user,
            request.args.get("category", "system"),
            request.args.get("scope_id"),
        )


def capabilities(user, category="system", scope_id=None):
    is_enabled = bool(svc.current_app.config.get("ENABLE_NOTIFICATIONS"))
    can_send = svc.can_publish(user, category, scope_id) if is_enabled else False
    creator = False
    if is_enabled and category == "team":
        creator = bool(
            TeamMember.objects(
                team=svc.oid(scope_id),
                user=user,
                status="active",
                base_tag="creator",
            ).first()
        )
    return {
        "enabled": is_enabled,
        "can_send": can_send,
        "can_view_sent": can_send
        or (is_enabled and svc.can_view_proofread(user, category, scope_id)),
        "can_manage": svc.administrator(user),
        "can_manage_policy": creator,
    }


class InboxAPI(MoeAPIView):
    @token_required
    def get(self):
        return inbox_page(self.current_user)


def inbox_page(user):
    svc.enabled()
    limit = page_size()
    pairs = []
    next_cursor = None
    for note, receipt in receipt_page(user):
        if len(pairs) == limit:
            next_cursor = str(pairs[-1][1].id)
            break
        pairs.append((note, receipt))
    rows = svc.detail_many(
        [n for n, _ in pairs],
        user,
        receipts={n.id: r for n, r in pairs},
        search_query=request.args.get("q", ""),
    )
    return {"items": rows, "next_cursor": next_cursor}


class UnreadAPI(MoeAPIView):
    @token_required
    def get(self):
        return unread_counts(self.current_user)


def unread_counts(user):
    if not svc.current_app.config.get("ENABLE_NOTIFICATIONS"):
        return {"total": 0, **{c: 0 for c in svc.CATEGORIES}}
    counts = {c: 0 for c in svc.CATEGORIES}
    for note, _ in receipt_page(user, unread_only=True, filtered=False):
        counts[note.category] += 1
    return {"total": sum(counts.values()), **counts}


class ReceiptAPI(MoeAPIView):
    @token_required
    def get(self, notification_id):
        svc.enabled()
        note, receipt = svc.get_receipt(self.current_user, notification_id)
        return svc.detail(note, self.current_user, receipt=receipt)

    @token_required
    def patch(self, notification_id):
        svc.enabled()
        note, receipt = svc.get_receipt(self.current_user, notification_id)
        data = body()
        if (
            not data
            or set(data) - {"read", "archived"}
            or any(type(v) is not bool for v in data.values())
        ):
            fail("只可修改本人的已读和归档状态")
        updates = {}
        if "read" in data:
            updates["set__read_at"] = svc.utcnow() if data["read"] else None
        if "archived" in data:
            updates["set__archived"] = data["archived"]
            if data["archived"]:
                updates["set__read_at"] = svc.utcnow()
        receipt.update(**updates)
        return {"ok": True}


class MarkReadAPI(MoeAPIView):
    @token_required
    def post(self):
        svc.enabled()
        data = body()
        if set(data) - {"category"} or data.get("category") not in (
            None,
            *svc.CATEGORIES,
        ):
            fail("分类无效")
        cutoff = svc.utcnow()
        changed = 0
        for note, receipt in receipt_page(self.current_user, unread_only=True):
            if data.get("category") and note.category != data["category"]:
                continue
            changed += NotificationReceipt.objects(
                id=receipt.id,
                user_id=self.current_user.id,
                created_at__lte=cutoff,
                read_at=None,
            ).update_one(set__read_at=cutoff)
        return {"changed": changed}


class PreferenceAPI(MoeAPIView):
    @token_required
    def get(self):
        row = NotificationPreference.objects(user_id=self.current_user.id).first()
        return {
            "categories": {
                **dict.fromkeys(svc.CATEGORIES, True),
                **(row.categories if row else {}),
            },
            "version": row.version if row else 0,
        }

    @token_required
    def put(self):
        data = body()
        categories = data.get("categories")
        if (
            set(data) != {"version", "categories"}
            or type(data["version"]) is not int
            or not isinstance(categories, dict)
            or set(categories) - set(svc.CATEGORIES)
            or any(type(v) is not bool for v in categories.values())
        ):
            fail("通知偏好无效")
        try:
            row = NotificationPreference.objects(
                user_id=self.current_user.id, version=data["version"]
            ).modify(upsert=True, new=True, set__categories=categories, inc__version=1)
        except NotUniqueError:
            fail("偏好版本冲突", 409)
        return {"version": row.version, "categories": row.categories}


class PolicyAPI(MoeAPIView):
    def permission(self, team_id, write=False):
        team_id = svc.oid(team_id)
        member = TeamMember.objects(
            team=team_id, user=self.current_user, status="active"
        ).first()
        if not member or (write and member.base_tag != "creator"):
            fail(
                "只有团队创建者可以调整通知发送权限" if write else "无权查看团队策略",
                403,
            )
        return team_id

    @token_required
    def get(self, team_id):
        team_id = self.permission(team_id)
        row = NotificationPolicy.objects(team_id=team_id).first()
        return {
            "team_admin": bool(row and row.team_admin),
            "project_admin": bool(row and row.project_admin),
            "version": row.version if row else 0,
        }

    @token_required
    def put(self, team_id):
        svc.enabled()
        team_id = self.permission(team_id, True)
        data = body()
        if (
            set(data) != {"version", "team_admin", "project_admin"}
            or type(data["version"]) is not int
            or type(data["team_admin"]) is not bool
            or type(data["project_admin"]) is not bool
        ):
            fail("通知权限设置无效")
        try:
            row = NotificationPolicy.objects(
                team_id=team_id, version=data["version"]
            ).modify(
                upsert=True,
                new=True,
                set__team_admin=data["team_admin"],
                set__project_admin=data["project_admin"],
                set__updated_by=self.current_user.id,
                inc__version=1,
            )
        except NotUniqueError:
            fail("设置版本冲突", 409)
        svc.audit(
            self.current_user.id, "policy", detail={"team_id": str(team_id), **data}
        )
        return {"version": row.version}


class PublishAPI(MoeAPIView):
    admin_surface = False

    @token_required
    def post(self):
        data = body()
        if self.admin_surface:
            svc.require_admin(self.current_user)
            if data.get("category") != "system" or data.get("scope_id"):
                fail("全站管理入口只能新建站点通知", 403)
        note = svc.publish(self.current_user, data, key())
        return svc.detail(note, self.current_user, management=True), 202


class PreviewAPI(MoeAPIView):
    admin_surface = False

    @token_required
    def post(self):
        data = body()
        if self.admin_surface:
            svc.require_admin(self.current_user)
            if data.get("category") != "system" or data.get("scope_id"):
                fail("管理入口不能代发团队或项目消息", 403)
        return svc.preview(self.current_user, data)


class CandidatesAPI(MoeAPIView):
    admin_surface = False

    @token_required
    def get(self):
        svc.enabled()
        svc.throttle(self.current_user, "candidates", 120)
        category = request.args.get("category", "system")
        if self.admin_surface:
            svc.require_admin(self.current_user)
            if category != "system":
                fail("管理入口只选择站点接收人", 403)
        data = {
            "category": category,
            "scope_id": request.args.get("scope_id"),
            "title": "接收人预览",
            "audience": {"mode": "all"},
        }
        note = svc.make_note(self.current_user, data)
        ids = svc.recipient_ids(note)
        query = User.objects(id__in=ids).only("id", "name")
        q = request.args.get("q", "").strip()
        if len(q) > 100:
            fail("搜索词过长")
        if q:
            query = query.filter(
                __raw__={
                    "$or": [
                        {"n": {"$regex": re.escape(q), "$options": "i"}},
                        {"as": {"$regex": re.escape(q), "$options": "i"}},
                    ]
                }
            )
        if request.args.get("cursor"):
            query = query.filter(id__gt=svc.oid(request.args["cursor"]))
        limit = page_size()
        rows = list(query.order_by("id").limit(limit + 1).max_time_ms(2000))
        return {
            "items": [{"id": str(u.id), "name": u.name} for u in rows[:limit]],
            "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
        }


class ManageListAPI(MoeAPIView):
    admin_surface = False

    @token_required
    def get(self):
        return management_page(self.current_user, self.admin_surface)


def management_page(user, admin=False):
    svc.enabled()
    query = Notification.objects
    if admin:
        svc.require_admin(user)
        svc.throttle(user, "admin_search", 120)
        svc.audit(user.id, "admin_search")
    else:
        # Ordinary sent history never becomes a global view for a site admin.
        category = request.args.get("category")
        scope_id = request.args.get("scope_id")
        can_send = svc.can_publish(user, category, scope_id)
        if not can_send and not svc.can_view_proofread(user, category, scope_id):
            fail("没有当前范围的发件查看权限", 403)
        query = query.filter(category=category)
        if not svc.scope_owner(user, category, scope_id):
            query = query.filter(actor_id=user.id)
        if not can_send:
            query = query.filter(event_type="proofread_feedback")
        if category == "team":
            query = query.filter(team_id=svc.oid(scope_id))
        elif category == "project":
            query = query.filter(project_id=svc.oid(scope_id))
        else:
            query = query.filter(actor_id=user.id)
    query = apply_search(query)
    delivery_state = request.args.get("delivery_state")
    if delivery_state:
        if delivery_state not in (
            "pending",
            "leased",
            "accepted",
            "retry_wait",
            "skipped",
            "failed",
            "unknown",
            "cancelled",
        ):
            fail("邮件状态无效")
        rows = list(
            NotificationDelivery._get_collection().aggregate(
                [
                    {"$match": {"state": delivery_state}},
                    {"$group": {"_id": "$notification_id"}},
                    {"$limit": 20001},
                ],
                maxTimeMS=2000,
            )
        )
        if len(rows) > 20000:
            fail("邮件状态检索超过安全上限", 422)
        ids = [row["_id"] for row in rows]
        query = query.filter(id__in=ids)
    if request.args.get("expired") in ("true", "false"):
        from mongoengine.queryset.visitor import Q

        query = (
            query.filter(expires_at__lte=svc.utcnow())
            if request.args["expired"] == "true"
            else query.filter(Q(expires_at=None) | Q(expires_at__gt=svc.utcnow()))
        )
    for field in ("team_id", "project_id", "actor_id"):
        if request.args.get(field):
            query = query.filter(**{field: svc.oid(request.args[field])})
    for field in ("state", "source", "event_type"):
        if request.args.get(field):
            query = query.filter(**{field: request.args[field][:100]})
    if request.args.get("revoked") in ("true", "false"):
        query = (
            query.filter(revoked_at__ne=None)
            if request.args["revoked"] == "true"
            else query.filter(revoked_at=None)
        )
    for arg, lookup in [("from", "created_at__gte"), ("to", "created_at__lte")]:
        if request.args.get(arg):
            query = query.filter(**{lookup: svc.date_value(request.args[arg])})
    if request.args.get("cursor"):
        query = query.filter(id__lt=svc.oid(request.args["cursor"]))
    limit = page_size()
    notes = list(
        query.exclude(
            "nodes",
            "body",
            "search_tokens",
            "payload.html",
            "payload.text",
        )
        .order_by("-id")
        .limit(limit + 1)
    )
    return {
        "items": svc.detail_many(
            notes[:limit],
            user,
            management=True,
            search_query=request.args.get("q", ""),
        ),
        "next_cursor": str(notes[limit - 1].id) if len(notes) > limit else None,
    }


class ManageDetailAPI(MoeAPIView):
    admin_surface = False

    @token_required
    def get(self, notification_id):
        svc.enabled()
        if self.admin_surface:
            svc.require_admin(self.current_user)
        note = svc.get_note(notification_id)
        if self.admin_surface:
            svc.audit(self.current_user.id, "admin_read", note)
        else:
            svc.ordinary_manage(self.current_user, note)
        return svc.detail(note, self.current_user, management=True)

    @token_required
    def patch(self, notification_id):
        svc.enabled()
        if self.admin_surface:
            fail("全站管理不能改写通知", 403)
        note = svc.edit_draft(self.current_user, svc.get_note(notification_id), body())
        return svc.detail(note, self.current_user, management=True)


class RevokeAPI(MoeAPIView):
    admin_surface = False

    @token_required
    def post(self, notification_id):
        svc.enabled()
        if self.admin_surface:
            svc.require_admin(self.current_user)
        note = svc.revoke(
            self.current_user,
            svc.get_note(notification_id),
            body(),
            key(),
            admin=self.admin_surface,
        )
        return svc.detail(note, self.current_user, management=True)


class CardSearchAPI(MoeAPIView):
    @token_required
    def get(self):
        svc.enabled()
        from app.models.project import Project

        svc.throttle(self.current_user, "cards", 120)
        q = request.args.get("q", "").strip()
        if len(q) > 100:
            fail("搜索词过长")
        query = Project.objects
        if q:
            query = query.filter(name__icontains=q)
        if request.args.get("cursor"):
            query = query.filter(id__gt=svc.oid(request.args["cursor"]))
        rows = list(
            query.only("id", "name", "team", "owner_user", "status")
            .order_by("id")
            .limit(100)
            .max_time_ms(2000)
        )
        result = []
        for project in rows:
            if self.current_user.can(project, "project:ACCESS"):
                result.append({"id": str(project.id), "name": project.name})
        return {
            "items": result,
            "next_cursor": str(rows[-1].id) if len(rows) == 100 else None,
        }


class CardAPI(MoeAPIView):
    @token_required
    def post(self):
        svc.enabled()
        data = body()
        ids = data.get("project_ids")
        if set(data) != {"project_ids"} or not isinstance(ids, list) or len(ids) > 5:
            fail("最多解析 5 个项目")
        return {
            "nodes": visible_nodes(
                [{"type": "project", "project_id": str(svc.oid(v))} for v in ids],
                self.current_user,
            )
        }


class NotificationSyncAPI(MoeAPIView):
    @token_required
    def get(self):
        view = request.args.get("view", "")
        if view not in (
            "",
            "inbox",
            "scope",
            "admin",
            "inbox_detail",
            "scope_detail",
            "admin_detail",
            "compose",
        ):
            fail("同步视图无效")
        user = self.current_user
        scoped = view in ("scope", "scope_detail", "compose") and request.args.get(
            "scope_id"
        )
        caps = capabilities(
            user,
            request.args.get("category", "system") if scoped else "system",
            request.args.get("scope_id") if scoped else None,
        )
        result = {
            "enabled": caps["enabled"],
            "capabilities": caps,
            "counts": unread_counts(user),
        }
        if caps["enabled"]:
            try:
                if view == "inbox":
                    result["page"] = inbox_page(user)
                elif view in ("scope", "admin"):
                    result["page"] = management_page(user, admin=view == "admin")
                elif view.endswith("_detail"):
                    note = svc.get_note(request.args.get("notification_id"))
                    if view == "inbox_detail":
                        note, receipt = svc.get_receipt(user, str(note.id))
                        result["notice"] = svc.detail(note, user, receipt=receipt)
                    else:
                        if view == "admin_detail":
                            svc.require_admin(user)
                            svc.audit(user.id, "admin_read", note)
                        else:
                            svc.ordinary_manage(user, note)
                        result["notice"] = svc.detail(note, user, management=True)
            except NotificationError as error:
                # Never echo cached private content on revocation or permission loss.
                result["view_error"] = {
                    "status": error.status_code,
                    "message": error.message,
                }
        revision = hashlib.sha256(
            json.dumps(result, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        if revision == request.args.get("revision"):
            result = {"unchanged": True, "revision": revision}
        else:
            result["revision"] = revision
        # Account-specific data must never be stored by the CDN/shared HTTP cache.
        return result, 200, {"Cache-Control": "no-store"}


notification = Blueprint("notification", __name__, url_prefix="/v1")


def route(path, cls, methods, name, admin=False):
    if admin:
        cls = type("Admin" + cls.__name__, (cls,), {"admin_surface": True})
    notification.add_url_rule(
        path, view_func=cls.as_view(name), methods=methods + ["OPTIONS"]
    )


route(
    "/me/notification-capabilities",
    NotificationCapabilitiesAPI,
    ["GET"],
    "notification_capabilities",
)
route("/me/notifications", InboxAPI, ["GET"], "notification_inbox")
route("/me/notifications/unread-counts", UnreadAPI, ["GET"], "notification_unread")
route("/me/notifications/mark-read", MarkReadAPI, ["POST"], "notification_mark_read")
route(
    "/me/notifications/<notification_id>",
    ReceiptAPI,
    ["GET", "PATCH"],
    "notification_receipt",
)
route(
    "/me/notification-preferences",
    PreferenceAPI,
    ["GET", "PUT"],
    "notification_preferences",
)
route(
    "/teams/<team_id>/notification-policy",
    PolicyAPI,
    ["GET", "PUT"],
    "notification_policy",
)
route("/notifications", PublishAPI, ["POST"], "notification_publish")
route("/notifications/preview", PreviewAPI, ["POST"], "notification_preview")
route("/notification-recipients", CandidatesAPI, ["GET"], "notification_candidates")
route("/notifications/sent", ManageListAPI, ["GET"], "notification_sent")
route(
    "/notifications/<notification_id>",
    ManageDetailAPI,
    ["GET", "PATCH"],
    "notification_manage",
)
route(
    "/notifications/<notification_id>/revoke",
    RevokeAPI,
    ["POST"],
    "notification_revoke",
)
route("/notification-project-cards/resolve", CardAPI, ["POST"], "notification_cards")
route("/admin/notifications", ManageListAPI, ["GET"], "notification_admin_list", True)
route("/admin/notifications", PublishAPI, ["POST"], "notification_admin_publish", True)
route(
    "/admin/notifications/preview",
    PreviewAPI,
    ["POST"],
    "notification_admin_preview",
    True,
)
route(
    "/admin/notification-recipients",
    CandidatesAPI,
    ["GET"],
    "notification_admin_candidates",
    True,
)
route(
    "/admin/notifications/<notification_id>",
    ManageDetailAPI,
    ["GET"],
    "notification_admin_detail",
    True,
)
route(
    "/admin/notifications/<notification_id>/revoke",
    RevokeAPI,
    ["POST"],
    "notification_admin_revoke",
    True,
)

route("/notification-project-cards", CardSearchAPI, ["GET"], "notification_card_search")

route("/me/notification-sync", NotificationSyncAPI, ["GET"], "notification_sync")
