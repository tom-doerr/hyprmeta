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
