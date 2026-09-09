"""Site-scoped User alias operations."""

from app.exceptions import NoPermissionError
from app.services.identity_audit import record_audit
from app.services.identity_permission import normalize_aliases


class UserAliasService:
    @staticmethod
    def _max_count():
        try:
            from flask import current_app

            return int(
                current_app.config.get(
                    "MAX_USER_ALIASES",
                    current_app.config.get("max_user_aliases", 10),
                )
            )
        except RuntimeError:
            return 10

    @classmethod
    def replace(
        cls, target, operator, aliases, *, default_display_name=None, request_id=None
    ):
        if operator is None or (operator != target and not operator.admin):
            raise NoPermissionError
        before = list(target.aliases or [])
        target.aliases = normalize_aliases(
            aliases,
            name=target.name,
            max_count=cls._max_count(),
        )
        if default_display_name is not None:
            target.default_display_name = (
                default_display_name.strip()
                if isinstance(default_display_name, str)
                else ""
            )
        if (
            target.default_display_name
            and target.default_display_name != target.name
            and target.default_display_name not in (target.aliases or [])
        ):
            target.default_display_name = ""
        target.save()
        event = record_audit(
            actor=operator,
            scope="user",
            action="user_aliases_replace",
            target_user=target,
            before={
                "scope": "site",
                "aliases": before,
                "default_display_name": getattr(target, "default_display_name", ""),
            },
            after={
                "scope": "site",
                "aliases": list(target.aliases),
                "default_display_name": target.default_display_name or "",
            },
            request_id=request_id,
        )
        return target, event
