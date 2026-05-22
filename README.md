# topiary

> *the art of shaping trees* — a configurable TUI dashboard for your terminal

`topiary` is a full-screen terminal dashboard inspired by `btop`. Each pane runs an
arbitrary shell command and refreshes on a configurable interval. Layout is defined
in a single TOML file.

```
┌─ System ──────────────────────────┬─ Market ──────────────────────────────┐
│ CPU  ▁▂▄▅▃▆▇▅▄▃   12.4%          │  AAPL  175.20  +1.2%                  │
│ MEM  [████████░░░░░░]  58.2%      │  GOOG  141.80  -0.3%                  │
│ NET  ↓    2.1 MB/s  ↑    0.4 MB/s │                                       │
├─ Dev ─────────────────────────────┴───────────────────────────────────────┤
│  Git  │  PRs  │  Remind                                                    │
│                                                                            │
│  main  ○ ← feature/auth   [!] feature/auth                                │
│  main  ●   fix/typo        [✓]                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

## Install

```bash
git clone https://github.com/you/topiary ~/src/topiary
cd ~/src/topiary
pip install -e . --no-build-isolation
```

## Usage

```bash
topiary examples/devdash.toml   # run with a specific config
topiary                          # looks for ./topiary.toml or ~/.config/topiary/topiary.toml
```

**Keybindings:**

| Key | Action |
|-----|--------|
| `q` / `ctrl+c` | Quit |
| `r` | Force-refresh all panes immediately |
| Tab (in tabs pane) | Switch tabs |

## Config

Config is a TOML file. Here is a complete annotated example:

```toml
[app]
title = "topiary · devdash"   # title shown in terminal window title

# Each [[rows]] entry is one horizontal strip.
# height: percentage ("40%") or fractional unit ("1fr" = take remaining space)

[[rows]]
height = "35%"

  # Each [[rows.panes]] entry is one column within the row.
  # width: percentage ("35%") or fractional unit ("1fr")

  [[rows.panes]]
  id      = "system"
  type    = "system"     # built-in: CPU sparkline + memory bar + network rates
  title   = "System"
  width   = "35%"

  [[rows.panes]]
  id        = "ticker"
  type      = "command"  # run any shell command, display stdout
  title     = "Market"
  command   = "ticker AUR"
  width     = "1fr"
  refresh   = 60          # re-run every N seconds (default: 5)
  min_width = 80          # hide this pane if terminal is narrower than 80 cols

[[rows]]
height = "1fr"

  [[rows.panes]]
  id    = "dev"
  type  = "tabs"         # tabbed group of command panes
  title = "Dev"
  width = "1fr"
  tabs = [
    { title = "Git",    command = "git_branch_tree",    refresh = 10 },
    { title = "PRs",    command = "git_branch_tree -p", refresh = 30 },
    { title = "Remind", command = "remind",              refresh = 5  },
  ]
```

### Pane types

| `type`    | Description |
|-----------|-------------|
| `command` | Run any shell command; display stdout; refresh on interval |
| `system`  | Built-in live stats: CPU sparkline, memory bar, network rates |
| `tabs`    | Tabbed container — each tab is a `command` pane |

### Tips

- Use **one-shot** variants of commands (not `-w` watch mode) — topiary handles the
  refresh loop itself via the `refresh` interval.
- `COLUMNS` and `LINES` env vars are set to the pane's current dimensions before
  each command run, so commands that respect them will format output correctly.
- Panes with `min_width` set automatically hide when the terminal is too narrow.

## Extending

The `examples/` directory has starter configs:

- `examples/minimal.toml` — system stats + df output
- `examples/devdash.toml` — system stats + ticker + git/PR/reminders

To add a new pane, just add a `[[rows.panes]]` entry with any shell command.

## Requirements

- Python 3.11+
- [textual](https://github.com/Textualize/textual) >= 0.80
- [psutil](https://github.com/giampaolo/psutil) >= 5 (for `type = "system"`)
- [rich](https://github.com/Textualize/rich) >= 12
