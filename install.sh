#!/usr/bin/env bash
# Install BetterBatteryLookup for the current user.
#   ./install.sh            daemon (systemd --user), `bbl` command, Plasma widget - no root
#   ./install.sh --system   also the root helper for GPU mode switching (uses sudo)
# Re-running upgrades in place; history and config are kept.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

APP="${XDG_DATA_HOME:-$HOME/.local/share}/betterbatterylookup/app"
BIN="$HOME/.local/bin"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
HELPER=/usr/local/lib/betterbatterylookup/bbl-gpumode
POLICY=/usr/share/polkit-1/actions/org.betterbatterylookup.gpumode.policy

ok()   { printf '  \e[32m✓\e[0m %s\n' "$*"; }
warn() { printf '  \e[33m!\e[0m %s\n' "$*"; }
die()  { printf '  \e[31m✗\e[0m %s\n' "$*" >&2; exit 1; }

echo "Checking dependencies"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "Python 3.11+ required"
python3 -c 'import gi; gi.require_version("Gio", "2.0"); from gi.repository import Gio' 2>/dev/null \
    || die "PyGObject missing (Arch: python-gobject, Debian/Ubuntu: python3-gi, Fedora: python3-gobject)"
ok "python3 + PyGObject"
python3 -c 'import gi; gi.require_version("GUdev", "1.0")' 2>/dev/null && ok "GUdev (monitor hotplug events)" \
    || warn "GUdev missing - monitor connect/disconnect events disabled (Arch: libgudev)"
python3 -c 'import textual' 2>/dev/null && ok "Textual (bbl TUI)" \
    || warn "Textual missing - 'bbl tui' unavailable (Arch: python-textual, others: pipx/pip install textual)"

echo "Installing app to $APP"
rm -rf "$APP"
mkdir -p "$APP" "$BIN" "$UNIT_DIR"
cp -r bbl bin "$APP/"
find "$APP" -name __pycache__ -prune -exec rm -rf {} +
ln -sf "$APP/bin/bbl" "$BIN/bbl"
ok "bbl -> $BIN/bbl"
case ":$PATH:" in *":$BIN:"*) ;; *) warn "$BIN is not in PATH" ;; esac

echo "Installing systemd user service"
cp systemd/betterbatterylookup.service "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable betterbatterylookup.service >/dev/null 2>&1
systemctl --user restart betterbatterylookup.service
sleep 1
systemctl --user is-active --quiet betterbatterylookup.service && ok "betterbatterylookup.service running" \
    || warn "service not running - see: journalctl --user -u betterbatterylookup"

if command -v kpackagetool6 >/dev/null; then
    echo "Installing Plasma widget"
    if kpackagetool6 -t Plasma/Applet -l 2>/dev/null | grep -qx org.betterbatterylookup.widget; then
        kpackagetool6 -t Plasma/Applet -u plasmoid >/dev/null && ok "widget upgraded (restart plasmashell to reload: systemctl --user restart plasma-plasmashell)"
    else
        kpackagetool6 -t Plasma/Applet -i plasmoid >/dev/null && ok "widget installed - add it: right-click panel > Add Widgets > BetterBatteryLookup"
    fi
else
    warn "kpackagetool6 not found - Plasma widget skipped (CLI and TUI still work)"
fi

if [[ "${1:-}" == "--system" ]]; then
    echo "Installing GPU mode helper (sudo)"
    sudo install -Dm755 helper/bbl-gpumode "$HELPER"
    sudo install -Dm644 helper/org.betterbatterylookup.gpumode.policy "$POLICY"
    ok "helper: $HELPER"
fi
echo "Done. Try: bbl status"
