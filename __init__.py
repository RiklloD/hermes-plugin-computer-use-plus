"""Computer Use Plus: Codex computer use for Hermes, with a live screen share in Hermes Desktop.

The model gets Open Computer Use's tools exactly as the MCP server defines them (`list_apps`, `get_app_state`, `query`,
`click`, `perform_secondary_action`, `scroll`, `drag`, `type_text`, `press_key`, `set_value`, `select_text`), loaded eagerly like
Codex's, plus the server's instructions in the system prompt. With the tools.override grant, Hermes's own cua-driver
`computer_use` tool is hidden so the model has one way to drive apps. The screen share, Stop and Take over are the
plugin's own (live.py, stream.py, desktop/plugin.js).
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from . import codex, commands, live, ocu, stream

logger = logging.getLogger(__name__)

TOOLSET = "computer_use"

# The private Hermes internals this plugin relies on. Checked by name at startup: a Hermes whose internals moved
# keeps its own computer_use instead of getting a half-working replacement.
_CONTRACT = {
    "tools.bot_desktop.lease": ("get", "acquire", "release", "assert_agent_may_act", "HumanHasControl", "HUMAN"),
    "tools.tool_search": ("_core_tool_names",),
}


def contract_problems() -> list:
    import importlib
    missing = []
    for mod_name, names in _CONTRACT.items():
        try:
            mod = importlib.import_module(mod_name)
        except Exception as e:
            missing.append(f"{mod_name} ({type(e).__name__})")
            continue
        missing += [f"{mod_name}.{n}" for n in names if not hasattr(mod, n)]
    return missing


def _keep_eager() -> None:
    """Codex lists its computer-use tools up front. Hermes defers plugin tools behind tool_search unless they are
    core, so count these as core for deferral (only deferral: `_core_tool_names` feeds nothing else)."""
    from tools import tool_search
    original = tool_search._core_tool_names
    if getattr(original, "_cup_eager", False):
        return

    def _core_tool_names() -> frozenset:
        return original() | frozenset(codex.TOOL_NAMES)
    _core_tool_names._cup_eager = True  # type: ignore[attr-defined]
    tool_search._core_tool_names = _core_tool_names


def _hidden() -> bool:
    return False


def _hidden_computer_use(args=None, **kwargs):
    return '{"error": "computer_use is replaced by the Computer Use tools (get_app_state, click, ...)."}'


def _prompt_section(info: Mapping[str, Any]) -> str:
    """The server's instructions, for sessions that have the computer_use toolset."""
    if not ocu.available():
        return ""
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.tools_config import _get_platform_tools
        platform = str(info.get("platform") or "cli")
        platform = "cli" if platform in ("cli", "desktop", "tui", "acp") else platform
        if TOOLSET not in _get_platform_tools(load_config_readonly(), platform, include_default_mcp_servers=False):
            return ""
    except Exception:
        logger.debug("computer-use-plus: toolset check for the prompt section failed", exc_info=True)
    return codex.instructions()


def on_session_end(session_id: str = "", **_: Any) -> None:
    if session_id:
        live.ended(session_id)
        try:
            codex.forget(session_id)
        except Exception:
            logger.debug("computer-use-plus: closing session %s failed", session_id, exc_info=True)


def on_turn_end(session_id: str = "", **_: Any) -> None:
    """Codex's turn-ended notification: the native cursor is put away."""
    if session_id:
        try:
            codex.turn_ended(session_id)
        except Exception:
            logger.debug("computer-use-plus: turn-ended notification failed", exc_info=True)


def register(ctx):
    if problems := contract_problems():
        logger.error("computer-use-plus: this Hermes lacks internals the plugin needs (%s); not registering",
                     ", ".join(problems))
        return
    if not ocu.available():
        logger.error("computer-use-plus: Open Computer Use is not installed at %s; Hermes keeps its own computer_use",
                     ocu.install_dir())
        return
    get_config = getattr(ctx, "get_config", None)
    if get_config is not None:
        try:
            live.configure(bool(get_config("live_preview", default=True)))
        except Exception:
            logger.debug("computer-use-plus: reading live_preview setting failed", exc_info=True)
    if live.active():
        stream.start()
    for name in codex.TOOL_NAMES:
        ctx.register_tool(name=name, toolset=TOOLSET, schema=codex.schema(name), handler=codex.handler_for(name),
                          check_fn=ocu.available, emoji=codex.emoji(name),
                          description=codex.schema(name)["description"])
    _keep_eager()
    has_capability = getattr(ctx, "has_capability", None)
    if has_capability is not None and has_capability("tools.override"):
        ctx.register_tool(name="computer_use", toolset=TOOLSET, schema={
            "name": "computer_use", "description": "Replaced by the Computer Use tools.",
            "parameters": {"type": "object", "properties": {}}},
            handler=_hidden_computer_use, check_fn=_hidden, override=True, emoji="🖥️",
            description="Hidden: replaced by Open Computer Use.")
    else:
        logger.warning("computer-use-plus: no tools.override grant, so Hermes's cua-driver computer_use stays "
                       "visible next to the Computer Use tools")
    register_section = getattr(ctx, "register_system_prompt_section", None)
    if register_section is not None:
        register_section("computer-use", _prompt_section)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("post_llm_call", on_turn_end)
    register_command = getattr(ctx, "register_command", None)
    if register_command is not None:
        register_command("computer-view", commands.handle,
                         description="Computer-use screen share: readiness check, stop, take over / hand back",
                         args_hint="[status|stop|takeover|handback]")
