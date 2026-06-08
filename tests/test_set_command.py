from __future__ import annotations

from yikes.commands import CommandContext, default_command_registry, default_model_registry
from yikes.domain import AgentSettings, Backend, Complexity, Driver
from yikes.runtime import DurableSessionManager, RuntimeKind, RuntimeRef
from yikes.session_icons import SessionIcons


class _Opts:
    def __init__(self):
        self.session_id = "sX"
        self.model = None
        self.complexity = Complexity.MEDIUM
        self.settings = AgentSettings()


class _Conv:
    def __init__(self):
        self.options = _Opts()

    def set_model(self, model):
        self.options.model = model

    def set_complexity(self, complexity):
        self.options.complexity = complexity

    def set_web_search(self, enabled):
        self.options.settings = AgentSettings(web_search_enabled=enabled)


def _context(conv):
    registry = default_command_registry()
    return registry, CommandContext(conv, registry, default_model_registry(), None)


def test_set_command_registered_with_aliases():
    registry = default_command_registry()
    spec = registry.find("set")
    assert spec is not None and "config" in spec.aliases and "cfg" in spec.aliases


def test_set_command_updates_session_record(tmp_path):
    # conftest isolates YIKES_RUNTIME_STORE, so the default store is the test's.
    DurableSessionManager().create(
        backend=Backend.CLAUDE, driver=Driver.DIRECT,
        runtime=RuntimeRef(kind=RuntimeKind.DIRECT), cwd=tmp_path, session_id="sX",
    )
    conv = _Conv()
    registry, ctx = _context(conv)
    spec = registry.find("set")
    spec.handler(ctx, "icon 🚀")
    spec.handler(ctx, "name=Cost Scanner")
    spec.handler(ctx, "owner michael")        # arbitrary custom key
    meta = SessionIcons().meta_for("sX")
    assert meta["icon"] == "🚀" and meta["name"] == "Cost Scanner"
    assert DurableSessionManager().get("sX").user_data["owner"] == "michael"


def test_set_command_routes_runtime_options(tmp_path):
    conv = _Conv()
    registry, ctx = _context(conv)
    spec = registry.find("set")
    spec.handler(ctx, "model haiku")
    spec.handler(ctx, "complexity=high")
    spec.handler(ctx, "web off")
    assert conv.options.model == "haiku"
    assert conv.options.complexity == Complexity.HIGH
    assert conv.options.settings.web_search_enabled is False


def test_set_command_no_arg_shows_usage(tmp_path):
    conv = _Conv()
    registry, ctx = _context(conv)
    result = registry.find("set").handler(ctx, "")
    assert "Usage: /set" in result.message
