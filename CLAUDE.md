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

## Agent tracking (`hyprmeta/agents.py`, run inside the daemon)

- State comes from the terminal TITLE first (Claude `✳`/`◐◓◑◒`, Codex braille /
  `task | dir` / `[ ! ] Action Required`), delivered as `windowtitlev2` events; the
  Codex rollout fallback must read the NEWEST open rollout (a process holds several).
- `AgentTracker` is pure (no I/O): feed it `update()` (full scan), `set_title()`,
  `focus()`, `tick()`; publish when `visible_state()` changes. Keep it that way — the
  tests drive it with fake clients and a fake `window_pid_for`.
- Attention semantics are USER DECISIONS: a stop must be stable `IDLE_CONFIRM_S`; only
  keyboard focus clears a flag (showing the workspace does not); a window focused while
  its agent finished is never flagged. Do not "simplify" these away.
- Tags are reconciled against `hyprctl clients -j` tags every full scan (never trust
  the daemon's memory alone). Hyprland 0.52 needs TWO `border_color` rules per tag —
  see README "Window borders" for the three parser bugs.
- Manual end-to-end test: a Python fake that sets comm via `prctl(PR_SET_NAME,
  b"claude")` and writes OSC 0 titles; ghostty only starts a `-e` command once its
  surface is visible (flash it floating + `no_initial_focus`, then
  `movetoworkspacesilent`). `pgrep -f` matches your own shell — match comm instead.
