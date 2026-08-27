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
    def replace(cls, target, operator, aliases, *, request_id=None):
        if operator is None or (operator != target and not operator.admin):
            raise NoPermissionError
        before = list(target.aliases or [])
        target.aliases = normalize_aliases(
            aliases,
            name=target.name,
            max_count=cls._max_count(),
        )
        target.save()
        event = record_audit(
            actor=operator,
            scope="user",
            action="user_aliases_replace",
            target_user=target,
            before={"scope": "site", "aliases": before},
            after={"scope": "site", "aliases": list(target.aliases)},
            request_id=request_id,
        )
        return target, event
