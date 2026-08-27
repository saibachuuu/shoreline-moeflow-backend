"""Small audit writer shared by identity services."""

from flask import has_request_context, request

from app.models.audit import IdentityAuditEvent


def current_request_id(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    if has_request_context():
        return request.headers.get("X-Request-ID", "")
    return ""


def record_audit(
    *,
    actor=None,
    scope: str,
    action: str,
    project=None,
    team=None,
    member=None,
    target_user=None,
    before=None,
    after=None,
    source="runtime",
    request_id=None,
    permission_sources=None,
) -> IdentityAuditEvent:
    event = IdentityAuditEvent(
        actor=actor,
        scope=scope,
        action=action,
        project_id=str(project.id) if project else None,
        team_id=str(team.id) if team else None,
        member_id=str(member.id) if member else None,
        target_user_id=str(target_user.id) if target_user else None,
        request_id=current_request_id(request_id),
        source=source,
        before=before or {},
        after=after or {},
        permission_sources=permission_sources or {},
    )
    return event.save()

