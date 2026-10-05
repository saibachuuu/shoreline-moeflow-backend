"""Optional public project lookup. The directory itself enables route registration."""

from app.modules import ModuleSpec


def _init(app):
    from .api import blueprint

    app.register_blueprint(blueprint)


MODULE = ModuleSpec(name="partner_search", init=_init)
