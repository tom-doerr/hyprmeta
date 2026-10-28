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
  already on target are skipped so a no-op switch sends nothing (unless a
  remembered cursor spot must still be applied: then just `movecursor`).
- **Cursor memory per meta (user request, Oct 7 2026):** `Store.cursors[name]` =
  where the cursor was when you last LEFT `name` (`App.leave` / `leave_here`); a
  RECORDED switch warps there (`movecursor` = warp + `simulateMouseMovement()`, so
  `follow_mouse = 1` focuses the window under it; no `focuswindow` needed).
  Previews and move+follow (`restore_cursor=False`) keep the cursor put. The daemon
  reads the origin spot in `_after_show` (after paint + first preview, never on the
  show path: `hyprctl` is a subprocess); every CLI switch path calls `leave_here()`.
- **Daemon commit order: commit BEFORE hide (`_run`).** The commit warps the cursor
  while the exclusive layer refuses focus; closing then focuses the window under
  the restored spot. Hide-first would briefly focus the window under the OLD spot,
  and a focus clears that window's agent-done flag.
- **Auto-commit rules = USER DECISIONS (Oct 7 2026), pure in `preview.auto_commit_delay`:**
  text matching exactly ONE meta → `unique_commit_s` (0.25 s, so a name typed on in one
  go stays in the picker, not in the window you land in); anything else (untouched,
  ambiguous, arrows, text deleted) NEVER closes on its own. The user disliked the
  picker closing mid-typing on the old "1 s after any key" rule.
- **The picker opens with NOTHING selected (user decision, Oct 22 2026):** an empty
  query selects no row in `_populate` (only an explicit `select=`, e.g. the rename
  return, does), so opening previews nothing. This retired the Oct 7 "tap the trigger
  = back to the previous meta" rule and its `auto_commit_s` knob (old configs that
  still carry the key load fine; it is ignored). `_move_selection` from no selection:
  Down = first row, Up = last. Enter with nothing selected does nothing.
- **Picker rename mode (user request, Oct 15 2026):** F2/Ctrl+R = selected meta,
  +Shift = the current one (not in the list). `_leave_rename` clears the entry
  while STILL in rename mode, so the `changed` repopulate shows only the hint; then
  one `_populate(select=…)` re-selects the old row. The other order selects (=
  previews = switches) the first row on the way back. After a rename, update
  `rows`, `current`, `PreviewSession.renamed` (else re-selecting it switches
  again) and call `_on_monitors`, which publishes whenever `visible_state()`
  changed (meta names included), so waybar shows the new name at once. A refused
  name stays in rename mode with a `hint-error` row; never close on an error.
- **Picker delete (user request, Oct 28 2026):** a trash button (`#trash`, mouse
  only, `can_focus` off) on rows whose meta has `windows == 0` in the tracker's
  `meta_summary`, and only after one successful full scan (`_scans > 0`: before it,
  every meta reads 0). The click calls `App.remove_empty`, which re-checks with a
  live `hyprctl clients` and refuses (red `hint-error` row atop the list) if a window
  is there now. A deleted meta that is being previewed goes back to the origin view
  (`PreviewSession.removed`). The current meta is not in the list, so it can't be deleted.
- **`Hypr._json` parses with `strict=False`:** Hyprland writes titles into its JSON
  with raw control characters; a strict parse failed ~300 `clients` scans a day
  (Oct 26-28 2026), each one a scan the agent tracker missed.
- `hyprctl activewindow` reports the monitor ID, `monitors` gives names; the
  ID→name join lives in `Hypr.active_window_monitor`.
- Menu protocol is dmenu-style (lines on stdin, chosen line on stdout, rc≠0 =
  cancel). Names must not contain tabs (the tab separates name from the
  workspace column in menu lines).
- **Daemon event socket (Oct 27 2026):** socket2 is read on its own thread
  (`MonitorCache._read_events`), never on the GTK loop. Hyprland DROPS a listener
  that falls 64 events behind (`Socket2 fd N overflowed event queue, removing` in
  hyprland.log), and the GTK loop blocks 5 s per `hyprctl` call that times out; a
  spinner title (~20 events/s) during a system stall got the daemon dropped. Batches
  queue under a lock and reach the loop through one pending `_deliver` idle. A lost
  socket sets `cache.lost` and `run()` returns 1: the unit is `Restart=on-failure`,
  which ignores exit 0, so never let a socket loss exit 0 again (it left the picker
  dead for good).
- Run `python3 -m pytest` before committing. `tests/test_daemon.py` needs
  pywayland and is skipped without it; run the full suite with the pipx venv's
  python (`pipx environment --value PIPX_LOCAL_VENVS`/hyprmeta/bin/python).

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
- Waybar name colour is a USER DECISION too (`name_color`): the count-weighted sRGB mean
  of the NON-idle marker colours (2 working + 1 finished = ⅔ green + ⅓ blue); all idle =
  the idle colour; no agents = the old dim/bright. Bold alone marks the current meta.
- Web agents (`WEB_KIND`, `contrib/chatgpt-status/`) are TITLE-ONLY: no process, so they
  must stay out of the process reconciliation in `update()` (else every scan would "exit"
  them) and a vanished `WEB_TAG` drops them WITHOUT a stamp. U+2063 survives Chromium →
  Wayland title (verified Sep 24). The extension's Stop-button selector is the fragile part:
  if ChatGPT changes its composer, only `content.js` needs updating.
- Tags are reconciled against `hyprctl clients -j` tags every full scan (never trust
  the daemon's memory alone). Hyprland 0.52 needs TWO `border_color` rules per tag —
  see README "Window borders" for the three parser bugs.
- Manual end-to-end test: a Python fake that sets comm via `prctl(PR_SET_NAME,
  b"claude")` and writes OSC 0 titles. Open the test terminal SMALL, floating and VISIBLE
  (`[workspace <visible> silent; float; size 360 110; no_initial_focus]`), then
  `movetoworkspacesilent`. BOTH traps are real (verified Sep 24): ghostty starts its command
  only after its surface first RENDERS, and a hidden workspace never renders (a small ghostty
  on hidden ws 99 mapped in 1 s, no child after 5 s; Alacritty spawns at once); and a LARGE
  new window can fail to map at all when the GB10 scanout carveout is full (kernel
  `NV_ERR_NO_MEMORY`). `pgrep -f` matches your own shell — match comm instead.
- **A `float; size` rule does NOT make a window's FIRST frame small.** Hyprland (0.52) sends
  the first configure from `predictSizeForNewWindow` = a split of the FOCUSED window, before
  any window rule is read (static rules are read at map). GTK 4.14 renders that first frame at
  ceil(mode width / logical width), which is 3x on rotated 1.25 monitors, and never retries a
  failed frame (it waits for a frame callback that needs a commit). Verify with
  `WAYLAND_DEBUG=1` and the `create_immed(..., w, h, ...)` lines.

## Layout snapshots + restore (`layout.py`, `restore.py`)

- Terminal foreground = the shell's `tpgid` (ghostty: ghostty → `sh -c zsh` → zsh; Alacritty:
  zsh directly). Claude session = `~/.claude/sessions/<pid>.json` (sessionId + cwd); Codex =
  UUID of the NEWEST open rollout (a process holds stale ones too). Keep `inspect_terminal`
  driven by the injectable `_stat/_cmdline/_cwd` so the fake-/proc tests keep working.
- One dir per Hyprland INSTANCE (start time + signature): a crash-reboot must never overwrite
  the pre-crash state; `--from previous` reads the last dir that is not this instance.
- Restore order is load-bearing (the small float only sizes the window AFTER map; its first
  frame still follows the focused window's tile, see above): launch small+floating on park ws 99 → SHOW ws 99 until every
  terminal's program has started (ghostty renders-before-spawn) → arrange (guillotine tree +
  verified re-insert, ported from `~/display-freeze-2026-08-17/restore_layout_aug17.py`) →
  `restore_view()` → refocus the caller's own terminal. `force_split=2` only during arrange,
  restored in `finally`. Never resume a session that is live (`live_sessions()`).
- The ARRANGE step moves focus and switches workspaces: never run it for a test while the user
  may be typing. Launch-only tests on the hidden park ws are safe (no focus change).
