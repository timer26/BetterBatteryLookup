import os

from . import APP_ID


def _xdg(var, fallback):
    return os.path.join(os.environ.get(var) or os.path.expanduser(fallback), APP_ID)


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", "~/.config")
DATA_DIR = _xdg("XDG_DATA_HOME", "~/.local/share")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.toml")
DB_FILE = os.path.join(DATA_DIR, "history.db")

# Root helper for GPU mode switching (installed by install.sh --system)
GPUMODE_HELPER = "/usr/local/lib/betterbatterylookup/bbl-gpumode"
