"""Config file handling: TOML with commented defaults, validated and merged."""
import copy
import logging
import os
import tomllib

from .paths import CONFIG_FILE

log = logging.getLogger(__name__)

DEFAULT_TOML = """\
# BetterBatteryLookup configuration.
# Changes are applied automatically while the daemon is running.

[panel]
show_lamp  = true        # colored status dot in the panel
show_watts = true        # current draw next to it, e.g. "12.3 W"

[plot]
tooltip_minutes = 10     # x-axis span of the hover plot
window_minutes  = 120    # x-axis span of the live plot in the TUI

[levels]
# Lamp color = the worse of (watts level, dGPU level). On AC the watts level is ignored.
yellow_watts     = 18.0
red_watts        = 30.0
dgpu_awake_idle  = "yellow"  # dGPU awake, not working: green|yellow|red
dgpu_busy        = "red"     # dGPU awake and working
gpu_busy_percent = 10        # NVIDIA utilization at or above this counts as busy

[estimate]
time_left_minutes = 5    # draw averaged over this many minutes for the main time-left estimate
                         # (KDE/UPower use roughly the last 30 s, so theirs jumps more)

[history]
keep_sessions = 20       # battery sessions kept on disk; older ones are deleted

[sampling]
interval_seconds  = 2    # how often power and GPU state are read (whole seconds)
nvml_poll_seconds = 30   # NVIDIA stats are read only while the dGPU is already awake,
                         # at most this often (each read delays its sleep ~10 s); 0 = never
flush_seconds     = 60   # how often samples are written to disk

[notify]
dgpu_idle_minutes = 3    # notify when the dGPU stays awake this long; 0 = off
session_summary   = true # summary notification when a battery session ends
sleep_drain       = true # notify battery lost during system sleep

[events]
high_draw_seconds  = 30            # log when draw stays above red_watts this long
battery_milestones = [50, 20, 10, 5]
"""

LEVELS = ("green", "yellow", "red")
DEFAULTS = tomllib.loads(DEFAULT_TOML)

_CHECKS = {
    ("levels", "dgpu_awake_idle"): lambda v: v in LEVELS,
    ("levels", "dgpu_busy"): lambda v: v in LEVELS,
    ("levels", "red_watts"): lambda v: v > 0,
    ("levels", "yellow_watts"): lambda v: v > 0,
    ("history", "keep_sessions"): lambda v: v >= 1,
    ("estimate", "time_left_minutes"): lambda v: v >= 1,
    ("sampling", "interval_seconds"): lambda v: v >= 1,
    ("sampling", "flush_seconds"): lambda v: v >= 1,
    ("plot", "tooltip_minutes"): lambda v: v >= 1,
    ("plot", "window_minutes"): lambda v: v >= 1,
    ("events", "battery_milestones"): lambda v: all(isinstance(x, (int, float)) for x in v),
}


def _same_type(default, value):
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, type(default))


def validate(raw):
    """Merge raw user config over defaults. Raises ValueError listing every bad key."""
    cfg = copy.deepcopy(DEFAULTS)
    errors = []
    for section, values in raw.items():
        if section not in cfg or not isinstance(values, dict):
            errors.append(f"unknown section [{section}]")
            continue
        for key, value in values.items():
            if key not in cfg[section]:
                errors.append(f"unknown key {section}.{key}")
            elif not _same_type(cfg[section][key], value):
                errors.append(f"{section}.{key}: expected {type(cfg[section][key]).__name__}")
            elif not _CHECKS.get((section, key), lambda v: True)(value):
                errors.append(f"{section}.{key}: invalid value {value!r}")
            else:
                cfg[section][key] = value
    if errors:
        raise ValueError("; ".join(errors))
    return cfg


def load(path=CONFIG_FILE):
    """Load config, writing the commented default file on first run."""
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(DEFAULT_TOML)
        log.info("wrote default config to %s", path)
    with open(path, "rb") as f:
        return validate(tomllib.load(f))
