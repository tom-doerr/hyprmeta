"""Live preview for the resident picker (pure logic, no GTK).

While the picker is open, the selected meta is shown on every monitor at once,
but NOT recorded as opened: scrolling through the list must not reorder it or
reset "last opened". Only `commit` records. `cancel` puts every monitor back to
exactly what it showed when the picker opened, including an off-grid layout.

Focus: while the picker holds the keyboard (layer-shell exclusive), Hyprland
refuses every window focus (FocusState.cpp: "Refusing a keyboard focus to a
window because of an exclusive ls"), so previews never move keyboard focus and
the agent tracker never sees a previewed window as "looked at". Hence the order
the daemon must keep: on cancel restore the workspaces BEFORE closing (so the
original window is visible and gets focus back); on commit close first (Hyprland
then focuses the window under the cursor on the meta you chose).
"""

from __future__ import annotations

from typing import Sequence

from .cli import App, Monitor


class PreviewSession:
    def __init__(self, app: App, origin: Sequence[Monitor], origin_window: str | None) -> None:
        self.app = app
        self.origin_ws = [m.active_ws for m in sorted(origin, key=lambda m: m.x)]
        self.origin_window = origin_window  # the window focused when the picker opened
        self.shown: str | None = None  # meta currently previewed (None = the origin view)
        self.closed = False

    def preview(self, name: str | None) -> bool:
        """Show meta `name` on every monitor now (not recorded). True if it switched."""
        if self.closed or name is None or name == self.shown:
            return False
        self.app.switch(name, record=False)
        self.shown = name
        return True

    def commit(self, name: str) -> list[int]:
        """Make `name` a real switch: shown (a no-op if already previewed) AND recorded."""
        self.closed = True
        targets = self.app.switch(name)
        self.shown = name
        return targets

    def cancel(self) -> None:
        """Back to exactly the workspaces the monitors showed when the picker opened."""
        self.closed = True
        if self.shown is not None:
            self.app.show_workspaces(self.origin_ws)
            self.shown = None
