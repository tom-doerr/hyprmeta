# hyprmeta — notes for agents

Public repo (github.com/tom-doerr/hyprmeta, MIT). Pure library/CLI code: keep
machine-specific detail (monitor descriptions, hostnames) OUT of this repo.

- Single module `hyprmeta/cli.py`; `Hypr` wraps `hyprctl` with an injectable
  runner, `Store` = names→offsets + MRU, `App` = the operations. Tests in
  `tests/test_cli.py` use `FakeHyprctl` and never touch a compositor.
- The current meta is DERIVED from live active workspaces (`current_offset`),
  never read from state. Do not "fix" a None there by falling back to the
  stored recent name — that would hide a drifted layout.
- `switch` emits ONE `hyprctl --batch`: per monitor `focusmonitor` +
  `focusworkspaceoncurrentmonitor`, then focus + cursor restore. Monitors
  already on target are skipped so a no-op switch sends nothing.
- `hyprctl activewindow` reports the monitor ID, `monitors` gives names; the
  ID→name join lives in `Hypr.active_window_monitor`.
- Menu protocol is dmenu-style (lines on stdin, chosen line on stdout, rc≠0 =
  cancel). Names must not contain tabs (the tab separates name from the
  workspace column in menu lines).
- Run `python3 -m pytest` before committing.
