import os
import re
import click
import logging
from app import flask_app
from app.factory import init_db
from app.migrations.runner import MigrationError, rollback, run_pending, status
from mongoengine.connection import get_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
    force=True,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


@click.group()
def main():
    pass


@click.command()
def docs():
    """
    需要安装apidoc, `npm install apidoc -g`
    """
    os.system("apidoc -i app/ -o docs/")


@click.command()
@click.option(
    "--check",
    "check_only",
    is_flag=True,
    help="Report pending migrations and exit non-zero when any exist.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print migrations that would run without changing the database.",
)
@click.option(
    "--level",
    type=click.Choice(["AUTO", "all"], case_sensitive=False),
    default="AUTO",
    show_default=True,
)
def migrate(check_only: bool, dry_run: bool, level: str):
    """
    Initialize the database and run versioned migrations.
    """
    db = get_db()
    try:
        if check_only or dry_run:
            # --check gates deploys, so it must see every pending migration
            # regardless of level; a level-filtered check would report "clean"
            # while a non-AUTO migration was still outstanding.
            migrations = run_pending(
                db, level="ALL" if check_only else level, dry_run=True
            )
            for migration in migrations:
                click.echo(
                    f"PENDING {migration.version} {migration.level} {migration.name}"
                )
            if check_only and migrations:
                raise click.ClickException(f"{len(migrations)} pending migration(s)")
            return

        init_db(flask_app)
        migrations = run_pending(db, level=level)
    except MigrationError as error:
        # This command gates container startup. A ClickException prints one
        # actionable line and exits 1; letting MigrationError escape would bury
        # the cause in a traceback.
        raise click.ClickException(str(error)) from error
    for migration in migrations:
        click.echo(f"APPLIED {migration.version} {migration.name}")


@click.command("migrate-status")
def migrate_status():
    """List every discovered migration and its applied state."""
    try:
        items = status(get_db())
    except MigrationError as error:
        raise click.ClickException(str(error)) from error
    for item in items:
        state = "APPLIED" if item["applied"] else "PENDING"
        click.echo(f"{state:7} {item['version']} {item['level']:6} {item['name']}")


@click.command("migrate-rollback")
@click.option(
    "--to",
    "target_version",
    required=True,
    help="Keep migrations up to and including this version.",
)
def migrate_rollback(target_version: str):
    """Rollback migrations newer than --to in reverse order."""
    try:
        migrations = rollback(get_db(), target_version)
    except MigrationError as error:
        raise click.ClickException(str(error)) from error
    for migration in migrations:
        click.echo(f"ROLLED BACK {migration.version} {migration.name}")


@click.command()
def list_translations():
    from app.factory import babel

    with flask_app.app_context():
        print(babel.list_translations())


@click.command("mit_file")
@click.option("--file", help="path to image file")
def mit_preprocess_file(file: str):
    from app.tasks.mit import preprocess_mit, MitPreprocessedImage

    proprocessed = preprocess_mit.delay(file, "CHT")
    proprocessed_result: dict = proprocessed.get()

    print("proprocessed", proprocessed_result)
    print("proprocessed", MitPreprocessedImage.from_dict(proprocessed_result))


@click.command("mit_dir")
@click.option("--dir", help="absolute path to a dir containing image files")
def mit_preprocess_dir(dir: str):
    from app.tasks.mit import preprocess_mit, MitPreprocessedImage

    for file in os.listdir(dir):
        if not re.match(r".*\.(jpg|png|jpeg)$", file):
            continue
        full_path = os.path.join(dir, file)
        proprocessed = preprocess_mit.delay(full_path, "CHT")
        proprocessed_result = MitPreprocessedImage.from_dict(proprocessed.get())

        print("proprocessed", proprocessed_result)
        for q in proprocessed_result.text_quads:
            print("text block", q.pts)
            print("  ", q.raw_text)
            print("  ", q.translated)


main.add_command(docs)
main.add_command(migrate)
main.add_command(migrate_status)
main.add_command(migrate_rollback)
main.add_command(list_translations)
main.add_command(mit_preprocess_file)
main.add_command(mit_preprocess_dir)

if __name__ == "__main__":
    main()
