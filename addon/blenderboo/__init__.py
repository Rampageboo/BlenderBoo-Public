"""BlenderBoo add-on and public API."""

from .version import ADDON_VERSION

bl_info = {
    "name": "BlenderBoo",
    "author": "BlenderBoo contributors",
    # Blender 5.2 statically parses bl_info with ast.literal_eval while
    # discovering add-ons, so this value must remain a literal.
    "version": (0, 10, 0),
    "blender": (4, 0, 0),
    "location": "Python API",
    "description": "Blender tools and Python execution for AI agents",
    "category": "Development",
}

if bl_info["version"] != ADDON_VERSION:
    raise RuntimeError("bl_info version must match ADDON_VERSION")

from .dispatcher import dispatch, tool_names, tool_specs
from .bridge import bridge_status, start_bridge, stop_bridge

import logging

_LOG = logging.getLogger(__name__)
_AUTO_START_SCHEDULED = False
_AUTO_START_CALLBACK = None
_LOAD_POST_CALLBACK = None

__all__ = [
    "dispatch",
    "tool_names",
    "tool_specs",
    "bridge_status",
    "start_bridge",
    "stop_bridge",
    "register",
    "unregister",
]


def register() -> None:
    """Register the bridge controls in Blender's 3D View sidebar."""
    global _LOAD_POST_CALLBACK
    from .bridge_ui import auto_start_enabled, register_bridge_ui
    from .tools._blender import get_bpy
    from .tools.versioning import observe_loaded_file

    bpy = get_bpy()
    if _LOAD_POST_CALLBACK is None:
        _LOAD_POST_CALLBACK = bpy.app.handlers.persistent(observe_loaded_file)
    if _LOAD_POST_CALLBACK not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_LOAD_POST_CALLBACK)
    # Blender restricts bpy.data during add-on registration. Anchor the loaded
    # file once registration has returned and normal data access is available.
    if not bpy.app.timers.is_registered(observe_loaded_file):
        bpy.app.timers.register(observe_loaded_file, first_interval=0.0)

    register_bridge_ui()
    if auto_start_enabled():
        _schedule_auto_start()


def _auto_start_bridge() -> None:
    global _AUTO_START_SCHEDULED, _AUTO_START_CALLBACK
    _AUTO_START_SCHEDULED = False
    _AUTO_START_CALLBACK = None
    try:
        if bridge_status()["running"]:
            return
        from .bridge_ui import auto_start_enabled
        from .config_token import resolve_or_create_bridge_token

        if not auto_start_enabled():
            return
        token = resolve_or_create_bridge_token()
        start_bridge(token, token_location="config")
    except Exception as exc:
        # Add-on registration must survive absent config, a competing port, and startup races.
        _LOG.warning("BlenderBoo bridge auto-start failed (%s): %s", type(exc).__name__, exc)


def _schedule_auto_start() -> None:
    global _AUTO_START_SCHEDULED, _AUTO_START_CALLBACK
    if _AUTO_START_SCHEDULED:
        return
    _AUTO_START_SCHEDULED = True
    try:
        from .tools._blender import get_bpy

        def callback():
            _auto_start_bridge()
            return None

        _AUTO_START_CALLBACK = callback
        get_bpy().app.timers.register(callback, first_interval=0.1)
    except Exception as exc:
        _AUTO_START_SCHEDULED = False
        _AUTO_START_CALLBACK = None
        _LOG.warning("BlenderBoo bridge auto-start could not be scheduled (%s): %s", type(exc).__name__, exc)


def unregister() -> None:
    """Stop bridge resources before disabling the add-on."""
    global _AUTO_START_SCHEDULED, _AUTO_START_CALLBACK, _LOAD_POST_CALLBACK
    try:
        from .tools._blender import get_bpy
        from .tools.versioning import observe_loaded_file

        timers = get_bpy().app.timers
        if timers.is_registered(observe_loaded_file):
            timers.unregister(observe_loaded_file)
    except Exception as exc:
        _LOG.warning("BlenderBoo file observation timer cleanup failed (%s): %s", type(exc).__name__, exc)
    if _LOAD_POST_CALLBACK is not None:
        try:
            from .tools._blender import get_bpy
            handlers = get_bpy().app.handlers.load_post
            if _LOAD_POST_CALLBACK in handlers:
                handlers.remove(_LOAD_POST_CALLBACK)
        except Exception as exc:
            _LOG.warning("BlenderBoo load handler cleanup failed (%s): %s", type(exc).__name__, exc)
        _LOAD_POST_CALLBACK = None
    if _AUTO_START_CALLBACK is not None:
        try:
            from .tools._blender import get_bpy

            timers = get_bpy().app.timers
            if timers.is_registered(_AUTO_START_CALLBACK):
                timers.unregister(_AUTO_START_CALLBACK)
        except Exception as exc:
            _LOG.warning("BlenderBoo bridge auto-start timer cleanup failed (%s): %s", type(exc).__name__, exc)
    _AUTO_START_SCHEDULED = False
    _AUTO_START_CALLBACK = None
    errors: list[str] = []
    if bridge_status()["running"]:
        try:
            stop_bridge()
        except Exception as exc:
            errors.append(f"bridge stop: {exc}")
    try:
        from .tools.background_render import shutdown_background_jobs

        shutdown_background_jobs()
    except Exception as exc:
        errors.append(f"background jobs shutdown: {exc}")
    try:
        from .bridge_ui import unregister_bridge_ui

        unregister_bridge_ui()
    except Exception as exc:
        errors.append(f"UI unregister: {exc}")
    if errors:
        raise RuntimeError("BlenderBoo unregister errors: " + "; ".join(errors))
