#!/usr/bin/env bash
# Remove BetterBatteryLookup.
#   ./uninstall.sh            remove app, service and widget; keep history and config
#   ./uninstall.sh --purge    also delete history and config
#   ./uninstall.sh --system   also remove the root GPU mode helper (uses sudo)
set -euo pipefail

DATA="${XDG_DATA_HOME:-$HOME/.local/share}/betterbatterylookup"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/betterbatterylookup"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/betterbatterylookup.service"

systemctl --user disable --now betterbatterylookup.service 2>/dev/null || true
rm -f "$UNIT" "$HOME/.local/bin/bbl" "$HOME/.local/bin/bbl-xfce-panel" \
    "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/betterbatterylookup.desktop"
systemctl --user daemon-reload
rm -rf "$DATA/app"
command -v kpackagetool6 >/dev/null && kpackagetool6 -t Plasma/Applet -r org.betterbatterylookup.widget >/dev/null 2>&1 || true
echo "Removed app, service and widget."

for arg in "$@"; do
    case "$arg" in
    --purge)
        rm -rf "$DATA" "$CONF"
        echo "Deleted history and config." ;;
    --system)
        if [ -e /etc/modprobe.d/betterbatterylookup-integrated.conf ]; then
            echo "Integrated-only mode is configured - switch back first: pkexec /usr/local/lib/betterbatterylookup/bbl-gpumode hybrid" >&2
            exit 1
        fi
        sudo rm -rf /usr/local/lib/betterbatterylookup /usr/share/polkit-1/actions/org.betterbatterylookup.gpumode.policy
        echo "Removed GPU mode helper." ;;
    esac
done
