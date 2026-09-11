"""Backfill missing and null tags on project members."""

from app.migrations.runner import IrreversibleMigration

VERSION = "0007"
NAME = "project_member_tags"
DESCRIPTION = "Backfill missing and null tags on project members"
LEVEL = "AUTO"


def up(db):
    db.project_member.update_many(
        {"$or": [{"tags": {"$exists": False}}, {"tags": None}]},
        {"$set": {"tags": []}},
    )


def down(db):
    raise IrreversibleMigration("Empty tag arrays cannot distinguish legacy documents")


def verify(db):
    return (
        db.project_member.count_documents(
            {"$or": [{"tags": {"$exists": False}}, {"tags": None}]}
        )
        == 0
    )
