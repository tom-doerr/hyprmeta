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
  "menu_new": "wofi --dmenu --conf {assets}/wofi.conf --style {assets}/wofi-new.css --exec-search -D dynamic_lines=true -D lines=3 --prompt \"＋ name for the new meta workspace\""
}
```

`base` lists one workspace per monitor, **left to right by x position**. `step`
is the default spacing between offsets. `menu` is any command that reads
candidate lines on stdin and prints the chosen line. `menu_new` is the same
kind of command used to ask for a new meta's name: it receives one hint line
(`ᴛʏᴘᴇ ᴀ ɴᴀᴍᴇ · ᴇɴᴛᴇʀ ᴄʀᴇᴀᴛᴇs ɪᴛ · ᴇsᴄ ᴄᴀɴᴄᴇʟs`) and must print what was
**typed**, which is what wofi's `--exec-search` does; the shipped stylesheet
tints that dialog mint so it never looks like the picker. `{assets}` expands
to the package's `assets/` directory.

## Hyprland binds

```ini
# Use the full path: `exec` binds run with the system PATH, which usually
# lacks ~/.local/bin where pipx installs commands.
bind = $mainMod, SPACE, exec, ~/.local/bin/hyprmeta pick                 # jump (or close an open picker)
bind = $mainMod SHIFT, SPACE, exec, ~/.local/bin/hyprmeta pick --move    # move the focused window there
```

Pressing the bind while a picker is open closes it instead of stacking a second
one (a pid file in `$XDG_RUNTIME_DIR` tracks the running picker's process
group).

## Frosted-glass look

The shipped `wofi.css` paints a translucent white pane; the blur behind it
comes from the compositor. Add a layer rule so Hyprland blurs wofi's surface
(Hyprland ≥ 0.51 block syntax):

```ini
layerrule {
    name = wofi-glass
    match:namespace = ^wofi$
    blur = true
    ignore_alpha = 0.1
}
```

Keep `ignore_alpha` below the pane's alpha (0.24 in `wofi.css`) or the pane is
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

- Entries are ordered most-recently-used first; the current one is marked.
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
