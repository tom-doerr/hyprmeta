# hyprmeta

Meta workspaces for [Hyprland](https://hypr.land): one **named offset**, applied to
**every monitor at once**, plus a fuzzy picker to jump between them by name.

If your three monitors normally show workspaces `4 5 6`, the meta workspace
`taxes` (offset 10) shows `14 15 16` on the same monitors, `music` (offset 20)
shows `24 25 26`, and so on. Each meta workspace is a whole desk, not a single
screen. Hyprland's own workspaces, binds and rules are untouched; hyprmeta only
issues `hyprctl dispatch` calls.

```
$ hyprmeta list
* taxes       10    14 15 16
  home         0    4 5 6
  music       20    24 25 26
```

## Install

```sh
pipx install git+https://github.com/tom-doerr/hyprmeta      # or:
pipx install --editable ~/git/hyprmeta
```

No dependencies beyond Python 3.10 and `hyprctl`. For the picker you need a
dmenu-compatible menu: `wofi` (default), `fzf`, `rofi -dmenu`, `fuzzel --dmenu`.

## Setup

While your monitors show the layout you want as the base (e.g. `4 5 6`):

```sh
hyprmeta init            # writes ~/.config/hyprmeta/config.json and a meta named "home" at offset 0
hyprmeta create taxes    # offset 10 (next free multiple of step)
hyprmeta switch taxes    # every monitor now shows 14 / 15 / 16
hyprmeta switch -        # back to the previous one
```

`config.json`:

```json
{
  "base": [4, 5, 6],
  "step": 10,
  "menu": "wofi --dmenu --conf {assets}/wofi.conf --style {assets}/wofi.css --prompt \"meta workspace\"",
  "menu_new": "wofi --dmenu --conf {assets}/wofi.conf --style {assets}/wofi-new.css --exec-search -D dynamic_lines=true -D lines=3 --prompt \"＋ name for the new meta workspace\"",
  "auto_commit_s": 1.0
}
```

`base` lists one workspace per monitor, **left to right by x position**. `step`
is the default spacing between offsets. `menu` is any command that reads
candidate lines on stdin and prints the chosen line. `menu_new` is the same
kind of command used to ask for a new meta's name: it receives one hint line
(`ᴛʏᴘᴇ ᴀ ɴᴀᴍᴇ · ᴇɴᴛᴇʀ ᴄʀᴇᴀᴛᴇs ɪᴛ · ᴇsᴄ ᴄᴀɴᴄᴇʟs`) and must print what was
**typed**, which is what wofi's `--exec-search` does; the shipped stylesheet
tints that dialog mint so it never looks like the picker. `{assets}` expands
to the package's `assets/` directory. `auto_commit_s` is how long the resident
picker waits for input before its live preview becomes the real switch
(`0` = never close on its own).

## The resident picker (recommended): 3–6 ms from keypress to pixels

`hyprmeta daemon` keeps a pre-built GTK layer-shell window alive and registers
two [Hyprland global shortcuts](https://github.com/hyprwm/hyprland-protocols)
(`hyprmeta:pick`, `hyprmeta:pick-move`). A keypress is then one Wayland event
into an already-running process — no shell, no interpreter start, no `hyprctl`.
Measured on a 120 Hz setup: **3.4–6.3 ms keypress→draw** (the one-shot wofi
path was ~90 ms plus a 200 ms fade).

```ini
bind = $mainMod, SPACE, global, hyprmeta:pick             # open (again = back where you were)
bind = $mainMod SHIFT, SPACE, global, hyprmeta:pick-move  # move the focused window
```

Run it as a user service (see `contrib/hyprmeta-daemon.service`; it waits for
`WAYLAND_DISPLAY` like any Wayland daemon started from
`graphical-session.target`):

```sh
systemctl --user enable --now hyprmeta-daemon.service
```

Needs `python3-gi`, `gir1.2-gtklayershell-0.1` and a pipx install that can see
them: `pipx install --system-site-packages --editable ~/git/hyprmeta`.

**Live preview.** Every monitor shows the SELECTED meta as soon as it is
selected, with the picker still open, so moving through the list flips your
whole desk. A preview is not an "open": it does not reorder the list or reset
"last opened". What makes it one:

- **No input for `auto_commit_s` (default 1 s):** the previewed meta becomes the
  real switch and the picker closes. Every key press restarts the timer. Since
  the list leaves out the meta you are on, a lone tap of the trigger returns you
  to the previous meta, like Alt+Tab.
- **Enter** or a click: the same, immediately.
- **Escape**, or the trigger again: every monitor goes back to exactly what it
  showed before, including an off-grid layout, and nothing is recorded.
- **Typed text matching no meta** (a new name, or a typo): the picker stays open,
  showing the last preview, until you press Enter (creates it) or Escape.

Other keys: type to fuzzy-filter, Up/Down/Tab to move, **Alt+Enter** moves the
window that was focused when the picker opened, **Shift+Enter** moves it and
follows. The move picker (`hyprmeta:pick-move`) never previews or auto-closes,
because an idle timeout must not move a window by accident. The `＋ new meta
workspace` row asks for a name.

Keyboard focus stays in the picker while it previews: Hyprland refuses window
focus while an exclusive layer surface is open. On close it focuses the window
under the cursor on the meta you chose, or your original window after Escape.
The current meta comes from a monitor snapshot kept fresh through Hyprland's
event socket, so opening the picker issues no compositor query.

It also listens on `$XDG_RUNTIME_DIR/hyprmeta.sock`: `toggle`, `show`,
`show-move`, `peek`, `hide` (= Escape, undoes a preview), `ping`, `quit`.
`peek` shows the picker without taking the keyboard and without previewing,
for screenshots and tests. A normal show grabs every keystroke, including
whatever someone is typing at that moment. `hyprmeta pick` uses the socket
when the daemon is running and falls back to the menu command below otherwise
(`--no-daemon` forces the menu).

## Agent status: which terminals run Claude Code / Codex, and who needs a look

The daemon also tracks coding agents per terminal window, with no hooks. Both
agents publish their state in the **terminal title**, and Hyprland delivers every
title change as an event, so no polling is involved:

| agent | working | stopped |
|---|---|---|
| Claude Code | `◐◓◑◒` spinner prefix | `✳ <topic>` |
| Codex | braille spinner `⠋⠙⠹…` | `<task> \| <dir>` (idle), `[ ! ] Action Required \| …` (waiting for approval) |
| ChatGPT (web) | `⏳` + U+2063 prefix | U+2063 prefix (invisible) |

When a Codex title says nothing, the newest rollout `.jsonl` the process holds
open decides (`task_started` / `task_complete`). `/proc` ancestry ties each agent
to its terminal window; Codex's `codex-linux-sandbox` helpers are skipped.

**ChatGPT in the browser** has no process per chat, so the page itself has to
say what it is doing. `contrib/chatgpt-status/` is a small Chromium extension
(one content script on chatgpt.com, no permissions). While the composer shows its
Stop button, it prefixes the tab title with `⏳` and the invisible U+2063, and
with U+2063 alone otherwise. The browser passes the title on as the window
title, so each chat window becomes an agent with the same markers, borders and
"finished since you looked" flag as a terminal. Install it with
`chrome://extensions` → Developer mode → **Load unpacked** → that directory,
then reload the open ChatGPT tabs. Limits: a window's title is its ACTIVE tab's
title, so keep a long-running chat as the active tab of its own window. Switching
tabs just hides the chat; it never counts as a finish.

**"Finished since you looked":** an agent seen working whose state then stays
stopped for 3 s (title flicker never counts), or that exits while working,
flags its window. The flag stays until **that window gets keyboard focus**;
switching to its workspace does not clear it. A window you were focused on when
its agent finished is never flagged, and an agent that starts working again
hides the flag until it stops again.

It shows up in three places, with the same markers everywhere:

| marker | meaning |
|---|---|
| `▶N` | N agents working |
| `✓N` | N windows whose agent finished (or died) since you last focused them |
| `!N` | N windows whose agent stopped to wait for you (e.g. an approval) |
| `○N` | N agents idle, nothing new |

Every marker glyph exists in JetBrains Mono, so each is exactly one cell wide.
That keeps the sidebar's name column straight. A fallback glyph such as `⟳`
renders 3 px wider and shifts its row.

1. **Picker rows**: the marks sit in a right-aligned column directly left of
   each name, so they read as belonging to it.
2. **A waybar sidebar**: `hyprmeta agents --waybar --follow` as a custom module
   prints one line per meta (markers right-aligned before the name, current
   meta in bold). The name takes the mean colour of its non-idle markers,
   weighted by count: two working and one finished give ⅔ green + ⅓ blue.
   All agents idle gives the idle grey. Its class is
   `attention`, `running` or `idle` for CSS. See `contrib/waybar-meta.jsonc`.
   Add `--row` to put every meta on one line instead, for a regular
   horizontal bar, and `--wrap CELLS` to continue on a new line before a line
   would pass that many characters (bar width ÷ font cell width, minus
   padding). Waybar grows the bar for the extra line and shrinks it back when
   the metas fit on one line again.
3. **Window borders**: the daemon tags terminals `agent-done` / `agent-running`
   (`tagwindow`, diffed every 2 s against the tags Hyprland really has) and
   window rules paint them:

```ini
# Hyprland 0.52 has three bugs in border_color rules: a block value loses its
# first token, the gradient form never fills the inactive colours, and the
# two-colour form never un-sets when the tag goes away. Two rules per tag,
# in this order, work around all three:
windowrule {
    name = agent-done
    match:tag = agent-done
    border_color = v0.52-drops-this rgb(89b4fa) rgb(89b4fa)   # active, inactive (blue = unread)
}
windowrule {
    name = agent-done-revert
    match:tag = agent-done
    border_color = v0.52-drops-this rgb(89b4fa)               # records the tag dependency
}
# … the same pair for agent-running (e.g. rgba(33ccffee) rgba(a6e3a1bb)).
# On a Hyprland whose parser is fixed: one rule per tag, no placeholder token.
```

Check a rule without looking at the screen:
`hyprctl getprop address:<window> inactive_border_color`.
`hyprmeta agents` prints the raw snapshot (`$XDG_RUNTIME_DIR/hyprmeta/agents.json`);
the daemon logs every finish (`journalctl --user -u hyprmeta-daemon | grep finished:`).

Not covered: agents inside tmux, or over ssh. Their processes do not descend
from the terminal window.

## Layout snapshots: where every window is, what every terminal runs

The daemon keeps a snapshot of the whole desktop on disk and updates it whenever
a window opens, closes or moves, or a terminal starts running something else.
Each window records its workspace, position, size, floating state and tab group.
Each terminal (ghostty or Alacritty) also records its working directory and
what runs in its foreground:

| foreground | recorded as | how it comes back |
|---|---|---|
| idle shell | `shell` | a shell in the same directory |
| a command (`btop`, `ssh -t nas 'tmux …'`) | its argv | the same command |
| Claude Code | the session id from `~/.claude/sessions/<pid>.json` | `claude --resume <id>` |
| Codex | the UUID of the newest rollout the process holds open | `codex resume <id>` |

Agents and commands are started inside `zsh -ic '…; exec zsh -i'`, so the
window keeps a shell when they exit.

```sh
hyprmeta layout show                # the live desktop, workspace by workspace
hyprmeta layout list                # saved snapshots, numbered, newest first
hyprmeta layout restore --dry-run   # what a restore would open, keep and skip
hyprmeta layout restore             # reopen + resume + rebuild the tiling
```

Snapshots live in `~/.local/state/hyprmeta/layouts/<start>_<instance>/`: one
directory per Hyprland session, so a crash-reboot can never overwrite the last
pre-crash state. Each holds `latest.json`, a ring of the last 30 distinct states
(undo for a window closed by mistake: `restore --from 1`), and one snapshot per
10 minutes for two days. `restore` defaults to `--from previous`, the final
state of the Hyprland session before this one. It also takes `latest`, a number
from `layout list`, or a file.

`restore` never resumes a session that is still running somewhere. It opens
each missing terminal small and floating on a parking workspace (99), shows
that workspace until every terminal's program has started, and then rebuilds
each affected workspace. It infers the dwindle split tree from the saved
rectangles, re-inserts the windows, verifying each split, rebuilds tab groups,
and resizes to the saved sizes. Browser windows are not launched (they restore
their own sessions); if one comes back with the same title, it is put back in its
cell. `--workspace N` limits a restore to one workspace.

Two ghostty facts shaped this. ghostty starts its program only after its
surface first renders, and a hidden workspace never renders. On NVIDIA GB10
machines, a new window's first buffer comes from a fixed scanout carveout, and
a large one can fail to allocate while a small one fits.

## Menu-command fallback (wofi)

```ini
# Use the full path: `exec` binds run with the system PATH, which usually
# lacks ~/.local/bin where pipx installs commands.
bind = $mainMod, SPACE, exec, ~/.local/bin/hyprmeta pick                 # jump (or close an open picker)
bind = $mainMod SHIFT, SPACE, exec, ~/.local/bin/hyprmeta pick --move    # move the focused window there
```

Pressing the bind while a wofi picker is open closes it instead of stacking a
second one (a pid file in `$XDG_RUNTIME_DIR` tracks the running picker's
process group).

## Frosted-glass look

The shipped `wofi.css` paints a translucent white pane; the blur behind it
comes from the compositor. Add a layer rule so Hyprland blurs wofi's surface
(Hyprland ≥ 0.51 block syntax):

```ini
layerrule {
    name = hyprmeta-glass
    match:namespace = ^(hyprmeta|wofi)$   # the daemon, and the wofi fallback
    blur = true
    ignore_alpha = 0.1
    no_anim = true      # a layersIn fade only adds latency to a picker
}
```

Keep `ignore_alpha` below the pane's alpha (0.22 in `wofi.css`) or the pane is
treated as see-through and gets no blur. Apply with `hyprctl reload
config-only`, which reloads the config without re-applying monitor modes.

Optional: make `Super+N` relative to the current meta workspace, so `Super+4`
shows `14` while you are in `taxes`:

```ini
bind = $mainMod, 4, exec, hyprmeta goto 4
bind = $mainMod SHIFT, 4, exec, hyprmeta moveto 4
```

Apply without restarting: `hyprctl keyword bind "SUPER, SPACE, exec, hyprmeta pick"`.

### fzf in a floating terminal

Set `"menu": "fzf --prompt 'meta> '"` and launch the picker inside a terminal,
then float it with a window rule:

```ini
bind = $mainMod, SPACE, exec, ghostty --class=hyprmeta-pick -e hyprmeta pick
windowrule {
    name = hyprmeta-pick
    match:class = ^hyprmeta-pick$
    float = true
    size = 40% 30%
    center = true
}
```

## Picker behaviour

- Entries are ordered by when you last opened them, most recent first, and the
  **current one is left out** — so the top line is the one you came from, and
  opening the picker and pressing Enter jumps straight back to it. Each line
  shows the name and how long ago it was opened (`just now`, `5 min ago`,
  `3 h ago`, `2 d ago`, …), never the workspace numbers — those are an
  implementation detail (`hyprmeta list` has them).
- The new-name dialog uses the same mint glass but has no search icon, a
  `＋ name for the new meta workspace` prompt and a dim hint row, so it never
  reads as a search box.
- Selecting `+ new meta workspace` prompts for a name and creates it at the next
  free offset.
- Typing a name that matches nothing and confirming it also creates it (menus
  that return the query text, such as `fzf --print-query` or wofi, do this in
  one step).
- Escape cancels; nothing changes.

## Commands

| Command | Effect |
|---|---|
| `init [--name home] [--step 10] [--force]` | write the config from the current monitor layout |
| `list` | metas, offsets and the workspaces they map to |
| `current` | name of the active meta (exit 1 if monitors disagree or the offset is unnamed) |
| `switch NAME [--create]` / `switch -` | show a meta on every monitor / go back |
| `create NAME [--offset N]` | define a meta |
| `remove NAME`, `rename OLD NEW` | bookkeeping only; windows are never touched |
| `move NAME [--follow]` | move the focused window to the same monitor slot in another meta |
| `pick [--move] [--follow] [--menu CMD]` | fuzzy picker |
| `goto N`, `moveto N [--silent]` | slot `N` relative to the current meta |
| `daemon` | the resident picker + agent tracking + layout snapshots (run it as a user service) |
| `agents [--waybar [--follow] [--row]]` | agent status per meta (the sidebar's data) |
| `layout save` / `show [--from …]` / `list` | snapshot now / print one / list saved ones |
| `layout restore [--from previous\|latest\|N\|FILE] [--workspace N] [--dry-run]` | reopen missing terminals, resume their sessions, rebuild their workspaces |

## How a switch works

For each monitor (left to right) whose active workspace is not already the
target, hyprmeta emits `focusmonitor <mon>; focusworkspaceoncurrentmonitor
<ws>`; that dispatcher pulls an existing workspace over from another monitor or
creates it if needed. The batch ends with `focusmonitor` back to the monitor
that had focus and `movecursor` to the cursor's previous position, so the
switch is invisible except for the workspaces changing. Everything goes out in
one `hyprctl --batch` call.

The "current" meta is always derived from the live active workspaces, never
from stored state, so pressing `Super+3` and drifting off the grid is reported
honestly (`current` fails, `list` shows no marker) instead of being guessed.

State: `~/.local/state/hyprmeta/state.json` (names, offsets, recency).
Override paths with `HYPRMETA_CONFIG` / `HYPRMETA_STATE`.

## Development

```sh
pipx install --editable .[dev]
python3 -m pytest
```

Tests run against a fake `hyprctl`; no compositor needed.

## License

MIT
