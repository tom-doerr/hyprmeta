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
original window is visible and gets focus back); on commit ALSO commit before
closing: the commit moves the cursor back to where you left that meta, and
closing then focuses the window under it (follow_mouse). Closing first would
focus whatever sits under the old cursor spot for a moment, marking it seen.
"""

from __future__ import annotations

from typing import Sequence

from .cli import App, Config, Monitor


def auto_commit_delay(cfg: Config, touched: bool, query: str, matches: Sequence[str]) -> float:
    """Seconds until the picker commits on its own; 0 = it waits for Enter / Escape.

    Untouched (opened, no key yet): `auto_commit_s`, so open + wait = back to the
    meta you came from. Typed text matching exactly ONE meta: `unique_commit_s`
    (the short pause keeps the rest of a name typed in one go in the picker
    instead of in the window you land in). Anything else never closes on its own.
    """
    if not touched:
        return cfg.auto_commit_s
    if query.strip() and len(matches) == 1:
        return cfg.unique_commit_s
    return 0.0


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
