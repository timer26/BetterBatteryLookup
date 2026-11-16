import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.extras as PlasmaExtras
import org.kde.kirigami as Kirigami
import org.kde.plasma.workspace.dbus as DBus
import org.kde.plasma.plasma5support as P5Support

PlasmoidItem {
    id: root

    property var st: null           // daemon GetState()
    property var details: null      // daemon GetGpuDetails(), fetched when the popup opens
    readonly property bool up: st !== null
    readonly property string bbl: '"$HOME/.local/bin/bbl"'
    readonly property bool showWatts: up ? st.panel.show_watts : false
    readonly property bool showLamp: up ? (st.panel.show_lamp || !st.panel.show_watts) : true

    preferredRepresentation: compactRepresentation
    Plasmoid.icon: "battery"

    // ---------------------------------------------------------------- helpers

    function levelColor(level) {
        switch (level) {
        case "green": return Kirigami.Theme.positiveTextColor
        case "yellow": return Kirigami.Theme.neutralTextColor
        case "red": return Kirigami.Theme.negativeTextColor
        default: return Kirigami.Theme.disabledTextColor
        }
    }
    function dur(s) {
        if (s === null || s === undefined) return "?"
        s = Math.round(s)
        if (s < 60) return s + " s"
        const m = Math.floor(s / 60)
        if (m < 60) return m + " m"
        return Math.floor(m / 60) + " h " + String(m % 60).padStart(2, "0") + " m"
    }
    function clock(ts) { return Qt.formatTime(new Date(ts * 1000), "hh:mm") }
    function gpuState(g) {
        if (!g) return ""
        return (g.busy ? "busy" : g.awake ? "awake" : "asleep") + " for " + dur(st.ts - g.since)
    }

    function call(member, cb) {
        const reply = DBus.SessionBus.asyncCall({
            service: "org.betterbatterylookup",
            path: "/org/betterbatterylookup",
            iface: "org.betterbatterylookup.Daemon",
            member: member,
            arguments: []
        })
        reply.finished.connect(() => cb(reply.isError ? null : JSON.parse(reply.value)))
    }
    function refresh() { call("GetState", d => root.st = d) }
    function refreshDetails() { call("GetGpuDetails", d => root.details = d) }
    function run(cmd) { exec.connectSource(cmd) }

    P5Support.DataSource {
        id: exec
        engine: "executable"
        onNewData: (source, data) => {
            disconnectSource(source)
            root.refresh()
            if (root.expanded) root.refreshDetails()
        }
    }

    Timer {
        interval: (root.up ? root.st.interval : 5) * 1000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.refresh()
    }

    onExpandedChanged: if (expanded) refreshDetails()

    // ------------------------------------------------------------------ panel

    compactRepresentation: MouseArea {
        Layout.minimumWidth: row.implicitWidth
        Layout.preferredWidth: row.implicitWidth
        hoverEnabled: true
        onClicked: root.expanded = !root.expanded

        RowLayout {
            id: row
            anchors.fill: parent
            spacing: Kirigami.Units.smallSpacing

            Rectangle {
                visible: root.showLamp
                Layout.alignment: Qt.AlignVCenter
                implicitWidth: Math.round(Kirigami.Units.iconSizes.small * 0.7)
                implicitHeight: implicitWidth
                radius: width / 2
                color: root.up ? root.levelColor(root.st.level) : "transparent"
                border.width: root.up ? 0 : 1
                border.color: Kirigami.Theme.disabledTextColor
            }
            PlasmaComponents.Label {
                visible: root.showWatts
                Layout.alignment: Qt.AlignVCenter
                // fixed width only for the changing watts number, so the panel does not jitter
                Layout.minimumWidth: root.up && root.st.watts !== null ? metrics.width : implicitWidth
                horizontalAlignment: Text.AlignRight
                text: !root.up ? "–" : root.st.watts !== null ? root.st.watts.toFixed(1) + " W" : "AC"
                TextMetrics { id: metrics; text: "88.8 W" }
            }
        }
    }

    // ---------------------------------------------------------------- tooltip

    toolTipItem: Item {
        implicitWidth: Kirigami.Units.gridUnit * 20
        implicitHeight: tip.implicitHeight
        Layout.minimumWidth: implicitWidth
        Layout.minimumHeight: implicitHeight
        Layout.maximumWidth: implicitWidth
        Layout.maximumHeight: implicitHeight

    ColumnLayout {
        id: tip
        anchors.left: parent.left
        anchors.right: parent.right
        spacing: Kirigami.Units.smallSpacing

        Kirigami.Heading {
            level: 4
            Layout.fillWidth: true
            Layout.margins: Kirigami.Units.smallSpacing
            text: !root.up ? "BetterBatteryLookup: daemon not running"
                 : root.st.watts !== null ? "Drawing " + root.st.watts.toFixed(1) + " W" : "On AC power"
        }
        PlasmaComponents.Label {
            visible: root.up
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.smallSpacing
            wrapMode: Text.Wrap
            text: !root.up ? "" : [
                root.st.has_battery ? "Battery " + Math.round(root.st.percent) + "% " + root.st.status
                    + (root.st.time_left_s ? ", " + root.dur(root.st.time_left_s) + " left" : "") : "",
                root.st.time_left_s ? "  " + root.st.time_left_minutes + " min avg: " + root.dur(root.st.time_left_s)
                    + (root.st.time_left_now_s ? " · now: " + root.dur(root.st.time_left_now_s) : "")
                    + (root.st.time_left_session_s ? " · session: " + root.dur(root.st.time_left_session_s) : "") : "",
                root.st.gpu ? "dGPU " + root.gpuState(root.st.gpu) : "",
                root.st.level_reasons.length ? "Level: " + root.st.level_reasons.join(", ") : ""
            ].filter(x => x).join("\n")
        }
        PowerPlot {
            visible: root.up
            Layout.fillWidth: true
            Layout.margins: Kirigami.Units.smallSpacing
            Layout.preferredHeight: Kirigami.Units.gridUnit * 5
            samples: root.up ? root.st.recent : []
            spanSeconds: root.up ? root.st.plot.tooltip_minutes * 60 : 600
            endTs: root.up ? root.st.ts : 0
            yellow: root.up ? root.st.thresholds.yellow : 18
            red: root.up ? root.st.thresholds.red : 30
        }
        PlasmaComponents.Label {
            visible: root.up
            Layout.leftMargin: Kirigami.Units.smallSpacing
            Layout.bottomMargin: Kirigami.Units.smallSpacing
            opacity: 0.7
            font: Kirigami.Theme.smallFont
            text: root.up ? "Last " + root.st.plot.tooltip_minutes + " min · shaded = dGPU awake" : ""
        }
    }
    }

    // ------------------------------------------------------------------ popup

    fullRepresentation: ColumnLayout {
        Layout.preferredWidth: Kirigami.Units.gridUnit * 24
        Layout.minimumWidth: Kirigami.Units.gridUnit * 20
        spacing: Kirigami.Units.smallSpacing

        PlasmaExtras.PlaceholderMessage {
            visible: !root.up
            Layout.fillWidth: true
            iconName: "battery-missing"
            text: "Daemon not running"
            explanation: "systemctl --user enable --now betterbatterylookup"
        }

        // header
        RowLayout {
            visible: root.up
            Rectangle {
                implicitWidth: Kirigami.Units.iconSizes.small
                implicitHeight: implicitWidth
                radius: width / 2
                color: root.up ? root.levelColor(root.st.level) : "transparent"
            }
            Kirigami.Heading {
                level: 3
                Layout.fillWidth: true
                text: !root.up ? "" : root.st.watts !== null
                      ? root.st.watts.toFixed(1) + " W · " + Math.round(root.st.percent) + "%"
                      : "On AC · " + (root.st.has_battery ? Math.round(root.st.percent) + "%" : "")
            }
            PlasmaComponents.ToolButton {
                icon.name: "configure"
                onClicked: root.run(root.bbl + " config")
                PlasmaComponents.ToolTip { text: "Edit config (applied automatically when saved)" }
            }
            PlasmaComponents.ToolButton {
                icon.name: "view-refresh"
                onClicked: { root.refresh(); root.refreshDetails() }
                PlasmaComponents.ToolTip { text: "Refresh (reads NVIDIA stats if the dGPU is awake)" }
            }
        }

        // dGPU
        Kirigami.Heading { visible: root.up && root.st.gpu; level: 5; text: "dGPU"; opacity: 0.7 }
        PlasmaComponents.Label {
            visible: root.up && root.st.gpu
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            text: {
                if (!root.up || !root.st.gpu) return ""
                const g = root.st.gpu, lines = [g.name, root.gpuState(g)]
                const n = root.details && root.details.nvml
                if (n) lines.push(`${n.pstate || ""} · ${n.power_w !== undefined ? n.power_w.toFixed(1) + " W" : ""} · ${n.util}% · ${n.temp_c} °C`)
                if (g.last_wake_cause) lines.push("Woke up because of: " + g.last_wake_cause)
                if (n && n.procs.length)
                    lines.push("Rendering on it: " + n.procs.map(p => p.name + (p.vram_mib !== null ? ` (${p.vram_mib} MiB)` : "")).join(", "))
                const h = root.details && root.details.holders
                if (h && Object.keys(h).length)
                    lines.push("Has it open: " + Object.values(h).join(", ") + " (privileged apps are hidden)")
                return lines.join("\n")
            }
        }

        // events
        Kirigami.Heading { visible: root.up; level: 5; text: "Recent events"; opacity: 0.7 }
        Repeater {
            model: root.up ? root.st.events : []
            delegate: RowLayout {
                required property var modelData
                Layout.fillWidth: true
                PlasmaComponents.Label {
                    text: root.clock(modelData.ts)
                    opacity: 0.7
                    Layout.alignment: Qt.AlignTop
                }
                PlasmaComponents.Label {
                    text: modelData.message
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
            }
        }

        // GPU mode
        Kirigami.Heading { visible: root.up && root.st.gpu_mode; level: 5; text: "GPU mode"; opacity: 0.7 }
        PlasmaComponents.Label {
            visible: root.up
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            text: {
                if (!root.up) return ""
                const m = root.st.gpu_mode
                let t = "Configured: " + m.configured + " · running: " + m.running
                if (m.pending_reboot) t += " — reboot to apply"
                if (!m.helper_installed) t += "\nSwitching needs the helper: ./install.sh --system"
                return t
            }
        }
        RowLayout {
            visible: root.up
            PlasmaComponents.Button {
                text: "Hybrid"
                icon.name: "video-display"
                enabled: root.up && root.st.gpu_mode.helper_installed && root.st.gpu_mode.configured !== "hybrid"
                onClicked: root.run(root.bbl + " gpu-mode hybrid")
                PlasmaComponents.ToolTip { text: "dGPU sleeps on its own and wakes when an app needs it (reboot)" }
            }
            PlasmaComponents.Button {
                text: "Integrated only"
                icon.name: "battery-profile-powersave"
                enabled: root.up && root.st.gpu_mode.helper_installed && root.st.gpu_mode.configured !== "integrated"
                onClicked: root.run(root.bbl + " gpu-mode integrated")
                PlasmaComponents.ToolTip { text: "dGPU completely off, nothing can wake it; dGPU display ports stop working (reboot)" }
            }
        }

        Item { Layout.fillHeight: true }

        PlasmaComponents.Button {
            Layout.fillWidth: true
            text: "Open details (sessions, timeline, battery)"
            icon.name: "utilities-terminal"
            onClicked: root.run(root.bbl + " open-tui")
        }
    }
}
