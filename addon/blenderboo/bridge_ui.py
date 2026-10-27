"""Blender UI for starting, stopping, and rotating the local bridge token."""

import secrets
from typing import Any

import bpy  # type: ignore[import-not-found]

from .bridge import bridge_status, start_bridge, stop_bridge
from .config_token import resolve_or_create_bridge_token, write_bridge_token

_SESSION_TOKEN: str | None = None
_REGISTERED = False


class CBTBridgeSettings(bpy.types.PropertyGroup):
    port: bpy.props.IntProperty(
        name="Port",
        description="Exclusive IPv4 loopback port for this Blender instance",
        default=9877,
        min=1024,
        max=65535,
    )
    pending_timeout: bpy.props.FloatProperty(
        name="Queue Timeout",
        description="Seconds a call may wait before Blender starts it",
        default=30.0,
        min=0.1,
        max=300.0,
    )


class CBTAddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    auto_start_bridge: bpy.props.BoolProperty(
        name="Auto-start bridge on load",
        description="Start the authenticated local bridge from the token stored in Codex config.toml",
        default=True,
    )

    def draw(self, context: Any) -> None:
        self.layout.prop(self, "auto_start_bridge")


def _settings(context: Any) -> CBTBridgeSettings:
    return context.window_manager.codex_blender_bridge


def _copy_token(context: Any) -> bool:
    if _SESSION_TOKEN is None:
        return False
    try:
        context.window_manager.clipboard = _SESSION_TOKEN
        return context.window_manager.clipboard == _SESSION_TOKEN
    except Exception:
        return False


def _discard_session_token(context: Any | None = None) -> bool:
    global _SESSION_TOKEN
    token = _SESSION_TOKEN
    clipboard_cleared = True
    if token is not None and context is not None:
        try:
            if context.window_manager.clipboard == token:
                context.window_manager.clipboard = ""
        except Exception:
            clipboard_cleared = False
    _SESSION_TOKEN = None
    return clipboard_cleared


def _config_token_for_start(*, rotate: bool = False) -> str:
    if rotate:
        token = secrets.token_urlsafe(32)
        write_bridge_token(token)
        return token
    return resolve_or_create_bridge_token()


class CBT_OT_bridge_start(bpy.types.Operator):
    bl_idname = "codex_blender.bridge_start"
    bl_label = "Start Bridge"
    bl_description = "Start this instance bridge and copy its new transient token"

    @classmethod
    def poll(cls, context: Any) -> bool:
        return not bridge_status()["running"]

    def execute(self, context: Any) -> set[str]:
        global _SESSION_TOKEN
        settings = _settings(context)
        try:
            token = _config_token_for_start()
            status = start_bridge(
                token,
                port=settings.port,
                pending_timeout=settings.pending_timeout,
                token_location="config",
            )
        except Exception as exc:
            self.report({"ERROR"}, f"Could not start bridge: {exc}")
            return {"CANCELLED"}
        _SESSION_TOKEN = token
        copied = _copy_token(context)
        suffix = "; token copied to clipboard" if copied else "; use Copy Token"
        self.report({"INFO"}, f"Bridge started on port {status['port']}{suffix}")
        return {"FINISHED"}


class CBT_OT_bridge_copy_token(bpy.types.Operator):
    bl_idname = "codex_blender.bridge_copy_token"
    bl_label = "Copy Token"
    bl_description = "Copy the current transient bridge token without displaying or logging it"

    @classmethod
    def poll(cls, context: Any) -> bool:
        return bridge_status()["running"] and _SESSION_TOKEN is not None

    def execute(self, context: Any) -> set[str]:
        if not _copy_token(context):
            self.report({"ERROR"}, "No panel-generated token is available")
            return {"CANCELLED"}
        self.report({"INFO"}, "Bridge token copied to clipboard")
        return {"FINISHED"}


class CBT_OT_bridge_stop(bpy.types.Operator):
    bl_idname = "codex_blender.bridge_stop"
    bl_label = "Stop Bridge"
    bl_description = "Stop this instance bridge and discard its in-memory token copy"

    @classmethod
    def poll(cls, context: Any) -> bool:
        return bridge_status()["running"]

    def execute(self, context: Any) -> set[str]:
        try:
            stop_bridge()
        except Exception as exc:
            clipboard_cleared = _discard_session_token(context)
            suffix = "" if clipboard_cleared else "; clipboard cleanup also failed"
            self.report({"ERROR"}, f"Bridge stopped with cleanup errors: {exc}{suffix}")
            return {"CANCELLED"}
        clipboard_cleared = _discard_session_token(context)
        if not clipboard_cleared:
            self.report({"WARNING"}, "Bridge stopped, but the clipboard could not be cleared")
        self.report({"INFO"}, "Bridge stopped")
        return {"FINISHED"}


class CBT_OT_bridge_rotate_token(bpy.types.Operator):
    bl_idname = "codex_blender.bridge_rotate_token"
    bl_label = "Rotate Token"
    bl_description = "Restart this instance bridge with a new transient token"

    @classmethod
    def poll(cls, context: Any) -> bool:
        return bridge_status()["running"]

    def execute(self, context: Any) -> set[str]:
        global _SESSION_TOKEN
        settings = _settings(context)
        try:
            stop_bridge()
        except Exception as exc:
            clipboard_cleared = _discard_session_token(context)
            suffix = "" if clipboard_cleared else "; clipboard cleanup also failed"
            self.report({"ERROR"}, f"Could not stop bridge for rotation: {exc}{suffix}")
            return {"CANCELLED"}
        clipboard_cleared = _discard_session_token(context)
        if not clipboard_cleared:
            self.report({"WARNING"}, "Old token clipboard cleanup failed before rotation")
        try:
            token = _config_token_for_start(rotate=True)
            status = start_bridge(
                token,
                port=settings.port,
                pending_timeout=settings.pending_timeout,
                token_location="config",
            )
        except Exception as exc:
            self.report({"ERROR"}, f"Bridge is stopped; token rotation restart failed: {exc}")
            return {"CANCELLED"}
        _SESSION_TOKEN = token
        copied = _copy_token(context)
        suffix = "; token copied to clipboard" if copied else "; use Copy Token"
        self.report({"INFO"}, f"Bridge token rotated on port {status['port']}{suffix}")
        return {"FINISHED"}


class CBT_PT_bridge(bpy.types.Panel):
    bl_label = "BlenderBoo Bridge"
    bl_idname = "CBT_PT_bridge"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BlenderBoo"

    def draw(self, context: Any) -> None:
        layout = self.layout
        settings = _settings(context)
        status = bridge_status()
        running = status["running"]

        column = layout.column()
        column.enabled = not running
        column.prop(settings, "port")
        column.prop(settings, "pending_timeout")

        if running:
            layout.label(text=f"Running on 127.0.0.1:{status['port']}", icon="CHECKMARK")
            layout.label(text=f"Instance {status['instance_id'][:8]}")
            row = layout.row(align=True)
            row.operator(CBT_OT_bridge_copy_token.bl_idname, icon="COPYDOWN")
            row.operator(CBT_OT_bridge_rotate_token.bl_idname, icon="FILE_REFRESH")
            layout.operator(CBT_OT_bridge_stop.bl_idname, icon="PAUSE")
            if status["registry_error"]:
                layout.label(text="Instance registry needs attention", icon="ERROR")
        else:
            layout.operator(CBT_OT_bridge_start.bl_idname, icon="PLAY")
            layout.label(text="Starting uses the Codex config token and copies it")


_CLASSES = (
    CBTAddonPreferences,
    CBTBridgeSettings,
    CBT_OT_bridge_start,
    CBT_OT_bridge_copy_token,
    CBT_OT_bridge_stop,
    CBT_OT_bridge_rotate_token,
    CBT_PT_bridge,
)


def register_bridge_ui() -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    registered: list[type] = []
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            registered.append(cls)
        bpy.types.WindowManager.codex_blender_bridge = bpy.props.PointerProperty(
            type=CBTBridgeSettings
        )
    except Exception as exc:
        cleanup_errors: list[str] = []
        for cls in reversed(registered):
            try:
                bpy.utils.unregister_class(cls)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"{cls.__name__}: {cleanup_exc}")
        if cleanup_errors:
            raise RuntimeError(
                f"Bridge UI registration failed: {exc}; cleanup errors: "
                + "; ".join(cleanup_errors)
            ) from exc
        raise
    _REGISTERED = True


def unregister_bridge_ui() -> None:
    global _REGISTERED
    token_cleanup_ok = _discard_session_token(getattr(bpy, "context", None))
    if not _REGISTERED:
        if not token_cleanup_ok:
            raise RuntimeError("Bridge UI unregister error: clipboard token cleanup failed")
        return
    errors: list[str] = []
    if not token_cleanup_ok:
        errors.append("clipboard token cleanup failed")
    if hasattr(bpy.types.WindowManager, "codex_blender_bridge"):
        try:
            del bpy.types.WindowManager.codex_blender_bridge
        except Exception as exc:
            errors.append(f"property cleanup: {exc}")
    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception as exc:
            errors.append(f"{cls.__name__}: {exc}")
    _REGISTERED = False
    if errors:
        raise RuntimeError("Bridge UI unregister errors: " + "; ".join(errors))


def auto_start_enabled() -> bool:
    try:
        preferences = bpy.context.preferences.addons[__package__].preferences
        return bool(preferences.auto_start_bridge)
    except Exception:
        return True
