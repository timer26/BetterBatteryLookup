# BetterBatteryLookup

Lightweight battery and dGPU power monitor for Linux laptops, with panel widgets for KDE Plasma 6 and Xfce.

- **Panel lamp** – green / yellow / red / grey by battery draw and dGPU state, optional watts next to it.
- **Hover** – current draw, battery, time left, dGPU state, small plot (shaded where the dGPU was awake).
- **Click** – who is using the dGPU, recent events, GPU mode buttons, button to open the TUI.
- **`bbl` TUI** – live plot, saved sessions, filterable timeline, battery health, GPU details.
- **Event log** – boot/shutdown, suspend/resume with battery drain, lid, screen lock, AC plug/unplug,
  session summaries, dGPU wake (with likely cause) and sleep, sustained high draw with top CPU users,
  battery milestones, monitor hotplug, crashes/gaps in data.
- **Notifications** – dGPU awake for too long, battery session summary, battery lost during sleep.

## How it stays light

- Reads only sysfs/procfs files (battery, PCI runtime-PM status, `/proc`), every 2 s by default.
- **Never wakes the dGPU**: its state comes from the kernel (`power/runtime_status`). NVIDIA stats (NVML)
  are read only when the dGPU is *already awake*, at most every `nvml_poll_seconds`, and when you open the
  popup / press `g` in the TUI. (Tools like btop or nvidia-smi poll NVML and keep the dGPU awake.)
- Samples are buffered and written to SQLite once a minute; old sessions are deleted automatically.
- One Python daemon samples the hardware. Plasma reads it over D-Bus. On Xfce, Generic Monitor runs a
  short `dbus-send` command to display the daemon's cached readings. The GTK details window runs only while open.

## Install

```sh
./install.sh            # daemon, `bbl`, desktop widgets (no root)
./install.sh --system   # + root helper for GPU mode switching (sudo)
```

**KDE Plasma 6** — Right-click the panel → *Add Widgets* → *BetterBatteryLookup*.

**Xfce** — Install the `xfce4-genmon-plugin` package, then right-click the panel → *Panel* →
*Add New Items* → *Generic Monitor*. In its *Properties*, paste the absolute command path printed by
`./install.sh` (normally `"/home/YOURNAME/.local/bin/bbl-xfce-panel"`), turn off *Label* and set
*Period* to **2 seconds**. A longer period updates the panel less often without changing hardware sampling.

Hover for draw, battery time, dGPU activity and a power history chart. Click for recent events,
GPU users, mode controls, configuration and the TUI. A hollow lamp means the daemon is unavailable.
The Xfce login entry starts the same systemd user service as Plasma.

Dependencies: Python ≥ 3.11, PyGObject. Optional: libgudev (monitor hotplug events),
Textual (TUI: `pacman -S python-textual`), kpackagetool6 (Plasma), Generic Monitor and `dbus-send`
(Xfce panel), GTK 3 PyGObject bindings (Xfce details, e.g. `gir1.2-gtk-3.0` on Debian/Ubuntu), pkexec (GPU mode).

Uninstall: `./uninstall.sh` (keeps history), `--purge` (deletes history and config), `--system` (helper).
On Xfce, also remove the Generic Monitor item you added to the panel.

## Usage

```sh
bbl                  # TUI (1-5 switch tabs, g = read dGPU users, r = refresh, q = quit)
bbl status           # one line; --json for everything
bbl log --today      # timeline; --since 2h|14:05|2026-11-16, --session N, --kind gpu_wake,resume
bbl sessions         # saved sessions
bbl export 12 -o s12.csv
bbl config           # edit config in $EDITOR and validate; --path, --check
bbl gpu-mode         # status | hybrid | integrated
```

Service logs: `journalctl --user -u betterbatterylookup`

## Configuration

`~/.config/betterbatterylookup/config.toml` – created with comments on first start, applied live when saved.
An invalid file is rejected (logged as an event) and the previous settings stay active.
See [config.example.toml](config.example.toml).

## Sessions

A **battery session** runs from unplugging (or booting on battery) until plugging in or shutting down.
Time on AC is stored as lighter **AC sessions** (events, no draw readings – the battery reports none while
charging). `history.keep_sessions` counts battery sessions; AC sessions between deleted ones go with them.

## GPU modes (NVIDIA)

| Mode | What it does |
|---|---|
| **Hybrid** (default) | dGPU runtime-suspends (~0 W) when unused and wakes when an app needs it. |
| **Integrated only** | NVIDIA driver blocked and dGPU removed from the PCI bus at boot – nothing can wake it. Display ports wired to the dGPU stop working. |

Switching rewrites `/etc/modprobe.d/betterbatterylookup-integrated.conf` and
`/etc/udev/rules.d/00-betterbatterylookup-integrated.rules`, rebuilds the initramfs and needs a reboot.
The helper refuses if the NVIDIA GPU drives the boot display (desktop, or a MUX set to dGPU).

**Recovery** if something goes wrong in integrated mode: boot, then `pkexec /usr/local/lib/betterbatterylookup/bbl-gpumode hybrid`
(or delete the two files above as root and rebuild the initramfs).

## Files

| Path | Content |
|---|---|
| `~/.local/share/betterbatterylookup/app/` | installed program |
| `~/.local/share/betterbatterylookup/history.db` | sessions, samples, events (SQLite) |
| `~/.config/betterbatterylookup/config.toml` | configuration |
| `~/.config/systemd/user/betterbatterylookup.service` | user service |

## Limitations

- Total draw comes from the battery, so there is no reading on AC.
- "Wake cause" is a correlation (processes started just before the wake), not proof. Privileged processes
  (kwin, btop with file capabilities) hide their open files from normal users.
- Integrated-only mode supports NVIDIA dGPUs. AMD/Intel dGPUs are monitored but not switched.
- Planned for v2: CPU / iGPU / RAM power split (Intel RAPL), screen brightness on the plot.
