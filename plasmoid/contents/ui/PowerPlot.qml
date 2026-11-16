import QtQuick
import org.kde.kirigami as Kirigami

// Watts over time; shaded bands where the dGPU was awake (yellow) or busy (red).
Canvas {
    id: plot

    property var samples: []        // [[ts, watts|null, gpuCode], ...]
    property real spanSeconds: 600
    property real endTs: Date.now() / 1000
    property real yellow: 18
    property real red: 30

    function fade(c, a) { return Qt.rgba(c.r, c.g, c.b, a) }

    onSamplesChanged: requestPaint()
    onWidthChanged: requestPaint()

    onPaint: {
        const ctx = getContext("2d")
        ctx.reset()
        const w = width, h = height, pad = Kirigami.Units.gridUnit * 1.6
        const t0 = endTs - spanSeconds
        let maxW = red * 1.1
        for (const s of samples) if (s[1] !== null && s[1] > maxW) maxW = s[1] * 1.1
        const x = ts => pad + (ts - t0) / spanSeconds * (w - pad)
        const y = v => h - 2 - v / maxW * (h - 14)
        const fg = Kirigami.Theme.textColor

        // dGPU bands
        for (let i = 0; i < samples.length; i++) {
            const g = samples[i][2]
            if (g < 1) continue
            const xa = x(samples[i][0])
            const xb = i + 1 < samples.length ? x(samples[i + 1][0]) : w
            ctx.fillStyle = g === 2 ? fade(Kirigami.Theme.negativeTextColor, 0.25)
                                    : fade(Kirigami.Theme.neutralTextColor, 0.25)
            ctx.fillRect(Math.max(xa, pad), 0, xb - Math.max(xa, pad), h)
        }

        // thresholds
        ctx.lineWidth = 1
        for (const [v, c] of [[yellow, Kirigami.Theme.neutralTextColor], [red, Kirigami.Theme.negativeTextColor]]) {
            ctx.strokeStyle = fade(c, 0.6)
            ctx.setLineDash([3, 3])
            ctx.beginPath(); ctx.moveTo(pad, y(v)); ctx.lineTo(w, y(v)); ctx.stroke()
        }
        ctx.setLineDash([])

        // watts line, broken where there is no reading (AC, sleep)
        ctx.strokeStyle = Kirigami.Theme.highlightColor
        ctx.lineWidth = 1.5
        ctx.beginPath()
        let pen = false, any = false
        for (const s of samples) {
            if (s[1] === null || s[0] < t0) { pen = false; continue }
            if (pen) ctx.lineTo(x(s[0]), y(s[1])); else ctx.moveTo(x(s[0]), y(s[1]))
            pen = any = true
        }
        ctx.stroke()

        // labels
        ctx.fillStyle = fade(fg, 0.7)
        ctx.font = Math.max(10, Math.round(Kirigami.Theme.smallFont.pointSize * 1.33)) + "px sans-serif"
        ctx.fillText(Math.round(maxW) + " W", 0, 10)
        ctx.fillText("0", 0, h - 2)
        if (!any) {
            ctx.fillStyle = fg
            ctx.fillText("no battery draw reading (on AC)", pad + 8, h / 2)
        }
    }
}
