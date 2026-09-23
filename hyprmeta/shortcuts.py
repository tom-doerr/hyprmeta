"""Zero-spawn triggers: register `hyprmeta:<id>` global shortcuts with Hyprland.

Hyprland's `hyprland-global-shortcuts-v1` protocol lets a client own named
shortcuts; a keybind then reads `bind = SUPER, SPACE, global, hyprmeta:pick`.
On the keypress the compositor sends a `pressed` event over the Wayland socket
— no shell, no process, no hyprctl.

This runs on its own Wayland connection in a background thread so it never
touches GTK's connection; callbacks must hop back to the GTK main loop.
"""

from __future__ import annotations

import threading
from typing import Callable

from pywayland.client import Display

from .protocols.hyprland_global_shortcuts_v1 import HyprlandGlobalShortcutsManagerV1

APP_ID = "hyprmeta"

Pressed = Callable[[str, int], None]  # (shortcut id, compositor timestamp in ns)


class GlobalShortcuts:
    def __init__(self, shortcuts: dict[str, tuple[str, str]], on_pressed: Pressed) -> None:
        """`shortcuts` maps id -> (description, trigger description)."""
        self._spec = shortcuts
        self._on_pressed = on_pressed
        self._display: Display | None = None
        self._manager = None
        self._objects: list = []  # keep proxies alive
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        display = Display()
        display.connect()
        self._display = display
        registry = display.get_registry()

        def on_global(reg, name, interface, version):  # noqa: ANN001
            if interface == HyprlandGlobalShortcutsManagerV1.name:
                self._manager = reg.bind(name, HyprlandGlobalShortcutsManagerV1, version)

        registry.dispatcher["global"] = on_global
        display.roundtrip()
        if self._manager is None:
            raise RuntimeError(
                "the compositor does not offer hyprland_global_shortcuts_manager_v1 "
                "(is this a Hyprland session?)"
            )
        for sid, (description, trigger) in self._spec.items():
            shortcut = self._manager.register_shortcut(sid, APP_ID, description, trigger)
            shortcut.dispatcher["pressed"] = self._handler_for(sid)
            self._objects.append(shortcut)
        display.roundtrip()
        self._thread = threading.Thread(target=self._loop, name="hyprmeta-shortcuts", daemon=True)
        self._thread.start()

    def _handler_for(self, sid: str):
        def handler(proxy, tv_sec_hi: int, tv_sec_lo: int, tv_nsec: int) -> None:  # noqa: ANN001
            ts_ns = ((tv_sec_hi << 32) | tv_sec_lo) * 1_000_000_000 + tv_nsec
            self._on_pressed(sid, ts_ns)

        return handler

    def _loop(self) -> None:
        assert self._display is not None
        while True:
            self._display.dispatch(block=True)
