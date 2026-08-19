import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Hyprland
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui
import "CaptionModel.js" as CaptionModel

Item {
  id: root

  // The host injects these after construction. They must not be `required`,
  // because Quattro only knows the plugin instance after Loader completes.
  property string omarchyPath: Quickshell.env("OMARCHY_PATH")
  property var shell: null
  property var manifest: null

  property bool opened: false
  property bool demoMode: false
  property string phase: "idle"
  property string selectedSource: "microphone"
  property string activeSource: "microphone"
  property var segments: []
  property real inputLevel: 0
  property int elapsedSeconds: 0
  property int lastLatencyMs: 0
  property string errorMessage: ""
  property string lastDiagnostic: ""

  property bool doctorSeen: false
  property bool doctorReady: false
  property string doctorMessage: ""
  property var doctorMissing: []
  property var doctorIssues: []

  property real fontScale: 1
  property int maxRows: 3
  property string captionPosition: "bottom"
  property bool controlsExpanded: true
  property bool explicitStop: false
  property bool sourceExplicit: false
  property bool doctorStartPending: false
  property bool doctorExpectedStop: false
  property bool watchStartPending: false
  property bool reopenPending: false
  property string reopenPayload: ""

  readonly property string pluginId: manifest && manifest.id
    ? String(manifest.id)
    : "io.github.josephbriones.live-captions"
  readonly property string sourceDir: manifest && manifest.__sourceDir ? String(manifest.__sourceDir) : ""
  readonly property string backendPath: sourceDir + "/bin/live-captions"
  readonly property bool activeState: ["starting", "listening", "recording", "paused", "stopping"].indexOf(phase) !== -1
  readonly property bool captionSurfaceVisible: opened && (demoMode || watchProcess.running || activeState)
  readonly property var visibleSegments: CaptionModel.visibleRows(segments, maxRows)
  readonly property string statusText: CaptionModel.statusLabel(phase, demoMode)
  readonly property string sourceText: activeSource === "desktop" ? "Desktop audio" : "Microphone"
  readonly property color statusColor: {
    if (phase === "error" || phase === "setup") return Color.urgent
    if (phase === "paused") return Color.muted
    if (activeState || demoMode) return Color.accent
    return Color.muted
  }
  readonly property string privacyText: {
    if (demoMode) return "Synthetic demo · no audio device is opened"
    if (watchProcess.running) return sourceText + " session active · on-device · no transcript file"
    if (watchStartPending) return "Starting local captions…"
    return "Nothing is recording. Capture requires an explicit Start action."
  }
  readonly property string setupDetails: {
    var parts = []
    if (doctorMessage) parts.push(doctorMessage)
    if (doctorIssues && doctorIssues.length > 0)
      parts.push(doctorIssues.join("\n"))
    if (doctorMissing && doctorMissing.length > 0)
      parts.push("Missing: " + doctorMissing.join(", "))
    if (parts.length === 0)
      parts.push("Install whisper-server and PipeWire tools, then add a local GGML Whisper model.")
    return parts.join("\n")
  }
  readonly property var controlScreen: {
    var screens = Quickshell.screens
    if (!screens || screens.length === 0) return null
    var monitor = Hyprland.focusedMonitor
    var wanted = monitor ? String(monitor.name || "") : ""
    if (wanted !== "") {
      for (var index = 0; index < screens.length; index++) {
        if (String(screens[index].name || "") === wanted) return screens[index]
      }
    }
    return screens[0]
  }

  function open(payloadJson) {
    var options = CaptionModel.parsePayload(payloadJson)
    if (opened && (doctorProcess.running || watchProcess.running || activeState)) {
      // A second summon reveals controls, but it must never swap an audio
      // source or turn a real capture into a demo mid-session.
      controlsExpanded = true
      return
    }
    if (!opened && (doctorProcess.running || watchProcess.running || doctorExpectedStop || explicitStop)) {
      // close() is asynchronous at the process boundary. Keep the requested
      // payload until the old child has actually left, so its final output
      // cannot leak into the next session.
      reopenPending = true
      reopenPayload = typeof payloadJson === "string" ? payloadJson : ""
      opened = true
      controlsExpanded = true
      phase = watchProcess.running ? "stopping" : "checking"
      return
    }
    demoMode = options.demo
    selectedSource = options.source
    activeSource = options.source
    sourceExplicit = options.sourceExplicit
    fontScale = options.fontScale
    maxRows = options.maxRows
    captionPosition = options.position
    controlsExpanded = true
    segments = []
    inputLevel = 0
    elapsedSeconds = 0
    lastLatencyMs = 0
    errorMessage = ""
    lastDiagnostic = ""
    doctorSeen = demoMode
    doctorReady = demoMode
    doctorMessage = ""
    doctorMissing = []
    doctorIssues = []
    explicitStop = false
    phase = demoMode ? "starting" : "checking"
    opened = true

    if (demoMode) {
      Qt.callLater(function() {
        if (root.opened && root.demoMode) {
          root.watchStartPending = true
          watchProcess.running = true
        }
      })
    } else {
      Qt.callLater(function() {
        if (root.opened && !root.demoMode) {
          root.doctorExpectedStop = false
          root.doctorStartPending = true
          doctorProcess.running = true
        }
      })
    }
  }

  function close() {
    opened = false
    reopenPending = false
    reopenPayload = ""
    segments = []
    inputLevel = 0
    watchStartPending = false
    doctorStartPending = false
    if (doctorProcess.running) {
      doctorExpectedStop = true
      doctorProcess.running = false
    }
    if (watchProcess.running) {
      explicitStop = true
      phase = "stopping"
      watchProcess.running = false
    } else {
      explicitStop = false
      phase = "idle"
    }
  }

  function resumePendingOpen() {
    if (!reopenPending || !opened || doctorProcess.running || watchProcess.running
        || doctorExpectedStop || explicitStop) return
    var payload = reopenPayload
    reopenPending = false
    reopenPayload = ""
    phase = "idle"
    Qt.callLater(function() {
      if (root.opened) root.open(payload)
    })
  }

  function dismiss() {
    if (shell && typeof shell.hide === "function") shell.hide(pluginId)
    else close()
  }

  function retryDoctor() {
    if (demoMode || doctorProcess.running) return
    doctorSeen = false
    doctorReady = false
    errorMessage = ""
    phase = "checking"
    doctorExpectedStop = false
    doctorStartPending = true
    doctorProcess.running = true
  }

  function beginCaptions() {
    // Deliberately no autostart path exists. This function is called only by
    // the Start button or explicit IPC while the already-open UI is visible.
    if (demoMode || !doctorReady || watchProcess.running) return
    activeSource = selectedSource
    segments = []
    elapsedSeconds = 0
    inputLevel = 0
    errorMessage = ""
    lastDiagnostic = ""
    explicitStop = false
    phase = "starting"
    watchStartPending = true
    watchProcess.running = true
  }

  function runAction(name) {
    if (demoMode || !watchProcess.running) return
    if (["pause", "resume", "stop"].indexOf(name) === -1) return

    errorMessage = ""
    if (name === "stop") {
      explicitStop = true
      phase = "stopping"
      watchProcess.running = false
      return
    }
    watchProcess.write(name + "\n")
  }

  function applyDoctor(event) {
    if (!opened || demoMode || doctorExpectedStop || !event || !event.valid) return
    doctorSeen = true
    doctorReady = event.ready === true
    doctorMessage = event.message || ""
    doctorMissing = event.missing || []
    doctorIssues = event.issues || []
    if (!sourceExplicit && event.source && event.source !== "unknown") {
      selectedSource = event.source
      activeSource = event.source
    }
    phase = doctorReady ? "idle" : "setup"
    errorMessage = doctorReady ? "" : doctorMessage
  }

  function applyEvent(event) {
    if (!opened || explicitStop || !event || !event.valid) return
    if (event.type === "doctor") {
      applyDoctor(event)
      return
    }
    if (event.type === "status") {
      phase = event.state
      if (!demoMode && event.source && event.source !== "unknown") activeSource = event.source
      elapsedSeconds = Math.max(elapsedSeconds, event.elapsedSeconds || 0)
      if (phase !== "error") errorMessage = ""
      return
    }
    if (event.type === "caption") {
      var segment = event.segment
      if (demoMode) {
        segment = {
          id: event.segment.id,
          startMs: event.segment.startMs,
          endMs: event.segment.endMs,
          text: event.segment.text,
          source: activeSource
        }
      }
      segments = CaptionModel.appendSegment(segments, segment, CaptionModel.MAX_SEGMENTS)
      lastLatencyMs = event.latencyMs || 0
      if (phase !== "paused") phase = "recording"
      return
    }
    if (event.type === "level") {
      inputLevel = event.value
      return
    }
    if (event.type === "heartbeat") {
      elapsedSeconds = Math.max(elapsedSeconds, event.elapsedSeconds || 0)
      if (event.state) phase = event.state
      return
    }
    if (event.type === "error") {
      errorMessage = event.message
      phase = "error"
    }
  }

  Process {
    id: doctorProcess
    command: [root.backendPath, "doctor"]
    stdout: StdioCollector {
      id: doctorStdout
      waitForEnd: true
      onStreamFinished: {
        if (!root.opened || root.demoMode || root.doctorExpectedStop) return
        var result = CaptionModel.parseCommandResult(text)
        var data = result.valid ? result.data : {
          ok: false,
          ready: false,
          message: result.message,
          missing: ["doctor response"]
        }
        data.type = "doctor"
        root.applyDoctor(CaptionModel.parseEvent(data))
      }
    }
    stderr: StdioCollector {
      id: doctorStderr
      waitForEnd: true
    }
    onStarted: root.doctorStartPending = false
    onRunningChanged: {
      if (running) return
      if (root.doctorExpectedStop) {
        root.doctorStartPending = false
        root.doctorExpectedStop = false
        Qt.callLater(function() { root.resumePendingOpen() })
        return
      }
      if (!root.doctorStartPending) return
      root.doctorStartPending = false
      if (root.opened && !root.demoMode && !root.doctorExpectedStop) {
        root.applyDoctor({
          valid: true,
          type: "doctor",
          ready: false,
          message: "Could not start the local caption helper. Check that bin/live-captions is installed and executable.",
          missing: []
        })
      }
      Qt.callLater(function() { root.resumePendingOpen() })
    }
    onExited: function(exitCode) {
      root.doctorStartPending = false
      if (root.doctorExpectedStop) {
        root.doctorExpectedStop = false
        Qt.callLater(function() { root.resumePendingOpen() })
        return
      }
      if (!root.opened || root.demoMode || root.doctorSeen) return
      root.applyDoctor({
        valid: true,
        type: "doctor",
        ready: false,
        message: CaptionModel.safeError("", doctorStderr.text, exitCode),
        missing: []
      })
    }
  }

  Process {
    id: watchProcess
    stdinEnabled: true
    command: root.demoMode
      ? [root.backendPath, "watch", "--demo", "--source", root.activeSource]
      : [root.backendPath, "watch", "--source", root.activeSource]
    stdout: SplitParser {
      onRead: function(line) { root.applyEvent(CaptionModel.parseEvent(line)) }
    }
    stderr: SplitParser {
      onRead: function(line) {
        if (root.opened && !root.explicitStop)
          root.lastDiagnostic = CaptionModel.cleanText(line, 500)
      }
    }
    onStarted: root.watchStartPending = false
    onRunningChanged: {
      if (running) return
      if (root.explicitStop) {
        root.watchStartPending = false
        root.explicitStop = false
        root.phase = "idle"
        root.elapsedSeconds = 0
        Qt.callLater(function() { root.resumePendingOpen() })
        return
      }
      if (!root.watchStartPending) return
      root.watchStartPending = false
      if (root.opened && !root.explicitStop) {
        root.errorMessage = "Could not start the local caption helper. Check that bin/live-captions is installed and executable."
        root.phase = "error"
      }
      Qt.callLater(function() { root.resumePendingOpen() })
    }
    onExited: function(exitCode) {
      root.watchStartPending = false
      root.inputLevel = 0
      if (root.explicitStop) {
        root.explicitStop = false
        root.phase = "idle"
        root.elapsedSeconds = 0
        Qt.callLater(function() { root.resumePendingOpen() })
        return
      }
      if (!root.opened) return
      if (root.demoMode) {
        if (exitCode !== 0) {
          root.errorMessage = root.lastDiagnostic || "The caption demo stopped unexpectedly."
          root.phase = "error"
        } else {
          root.phase = "idle"
        }
        return
      }
      if (["starting", "listening", "recording", "paused"].indexOf(root.phase) !== -1) {
        root.errorMessage = root.lastDiagnostic || "The local caption process stopped unexpectedly."
        root.phase = "error"
      }
    }
  }

  IpcHandler {
    target: "io.github.josephbriones.live-captions"

    function open(payloadJson: string): string {
      root.open(payloadJson)
      return "ok"
    }
    function close(): string {
      root.dismiss()
      return "ok"
    }
    function start(): string {
      if (!root.opened) return "closed"
      if (root.demoMode) return watchProcess.running ? "demo-running" : "demo-finished"
      if (!root.doctorReady) return "not-ready"
      if (watchProcess.running) return "already-running"
      root.beginCaptions()
      return "ok"
    }
    function pause(): string {
      if (root.demoMode) return "demo-read-only"
      if (!watchProcess.running) return "not-recording"
      if (root.phase !== "listening" && root.phase !== "recording") return "not-recording"
      root.runAction("pause")
      return "ok"
    }
    function resume(): string {
      if (root.demoMode) return "demo-read-only"
      if (!watchProcess.running) return "not-paused"
      if (root.phase !== "paused") return "not-paused"
      root.runAction("resume")
      return "ok"
    }
    function stop(): string {
      if (root.demoMode) return "demo-read-only"
      if (!watchProcess.running) return "not-recording"
      root.runAction("stop")
      return "ok"
    }
    function state(): string {
      return JSON.stringify({
        open: root.opened,
        state: root.phase,
        source: root.activeSource,
        demo: root.demoMode,
        running: watchProcess.running,
        ready: root.doctorReady,
        segmentCount: root.segments.length
      })
    }
    function ping(): string { return "ok" }
  }

  // Captions are visual-only on every output. The empty input region is the
  // key accessibility invariant: users keep clicking and typing in the app
  // beneath the overlay while text remains readable above it.
  Variants {
    model: Quickshell.screens

    PanelWindow {
      id: captionWindow
      required property var modelData
      screen: modelData
      visible: root.captionSurfaceVisible
      anchors { top: true; right: true; bottom: true; left: true }
      color: "transparent"
      exclusionMode: ExclusionMode.Ignore
      WlrLayershell.namespace: "live-captions-text"
      WlrLayershell.layer: WlrLayer.Overlay
      WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
      mask: Region {}

      BorderSurface {
        id: captionCard
        visible: root.captionSurfaceVisible
        width: Math.min(Style.space(900), Math.max(Style.space(300), captionWindow.width - Style.space(48)))
        height: captionColumn.implicitHeight + Style.space(28) + borderTop + borderBottom
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: root.captionPosition === "top" ? parent.top : undefined
        anchors.bottom: root.captionPosition === "bottom" ? parent.bottom : undefined
        anchors.topMargin: root.captionPosition === "top" ? Style.space(32) : 0
        anchors.bottomMargin: root.captionPosition === "bottom" ? Style.space(64) : 0
        color: Util.alpha(Color.popups.background, 0.94)
        borderSpec: Border.surfaceSpec("popups", "border", Color.popups.border, Math.max(1, Style.space(2)))
        radius: Style.cornerRadius

        Column {
          id: captionColumn
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          anchors.leftMargin: captionCard.borderLeft + Style.space(18)
          anchors.rightMargin: captionCard.borderRight + Style.space(18)
          spacing: Style.space(8)

          Row {
            spacing: Style.space(8)
            Rectangle {
              width: Style.space(8)
              height: width
              radius: width / 2
              anchors.verticalCenter: parent.verticalCenter
              color: root.statusColor
            }
            Text {
              text: root.statusText + " · " + root.sourceText
              color: Util.alpha(Color.popups.text, 0.76)
              font.family: Style.font.family
              font.pixelSize: Math.round(Style.font.caption * root.fontScale)
              font.bold: true
            }
          }

          Text {
            visible: root.visibleSegments.length === 0
            width: parent.width
            text: root.phase === "paused"
              ? "Captions paused"
              : "Listening… the first caption appears after a short local audio chunk."
            color: Color.popups.text
            font.family: Style.font.family
            font.pixelSize: Math.round(Style.font.title * root.fontScale)
            font.bold: true
            wrapMode: Text.WordWrap
            horizontalAlignment: Text.AlignHCenter
          }

          Repeater {
            model: root.visibleSegments

            delegate: Column {
              required property var modelData
              required property int index
              width: captionColumn.width
              spacing: Style.space(2)
              opacity: index === root.visibleSegments.length - 1 ? 1 : 0.72

              Text {
                text: CaptionModel.speakerLabel(modelData)
                textFormat: Text.PlainText
                color: root.statusColor
                font.family: Style.font.family
                font.pixelSize: Math.round(Style.font.caption * root.fontScale)
                font.bold: true
              }
              Text {
                width: parent.width
                text: modelData.text
                textFormat: Text.PlainText
                color: Color.popups.text
                font.family: Style.font.family
                font.pixelSize: Math.round(Style.font.title * root.fontScale)
                font.bold: index === root.visibleSegments.length - 1
                wrapMode: Text.WordWrap
                maximumLineCount: 3
                elide: Text.ElideRight
              }
            }
          }
        }
      }
    }
  }

  // One small interactive card follows the Hyprland-focused output. Its mask
  // contains only the card, so the rest of this full-screen surface remains
  // click-through just like the caption surfaces above.
  PanelWindow {
    id: controlWindow
    screen: root.controlScreen
    visible: root.opened && root.controlScreen !== null
    anchors { top: true; right: true; bottom: true; left: true }
    color: "transparent"
    exclusionMode: ExclusionMode.Ignore
    WlrLayershell.namespace: "live-captions-controls"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
    mask: Region { item: controlCard }

    BorderSurface {
      id: controlCard
      width: Math.min(root.controlsExpanded ? Style.space(410) : Style.space(285), controlWindow.width - Style.space(32))
      height: controlColumn.implicitHeight + Style.space(24) + borderTop + borderBottom
      anchors.right: parent.right
      anchors.rightMargin: Style.space(16)
      anchors.top: root.captionPosition === "bottom" ? parent.top : undefined
      anchors.bottom: root.captionPosition === "top" ? parent.bottom : undefined
      anchors.topMargin: root.captionPosition === "bottom" ? Style.space(16) : 0
      anchors.bottomMargin: root.captionPosition === "top" ? Style.space(16) : 0
      color: Util.alpha(Color.popups.background, 0.97)
      borderSpec: Border.surfaceSpec("popups", "border", Color.popups.border, Math.max(1, Style.space(2)))
      radius: Style.cornerRadius

      Column {
        id: controlColumn
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        anchors.leftMargin: controlCard.borderLeft + Style.space(12)
        anchors.rightMargin: controlCard.borderRight + Style.space(12)
        spacing: Style.space(10)

        RowLayout {
          width: parent.width
          spacing: Style.space(8)

          Rectangle {
            Layout.alignment: Qt.AlignVCenter
            width: Style.space(9)
            height: width
            radius: width / 2
            color: root.statusColor
          }
          Column {
            Layout.fillWidth: true
            spacing: 0
            Text {
              text: "Live Captions"
              color: Color.popups.text
              font.family: Style.font.family
              font.pixelSize: Style.font.subtitle
              font.bold: true
            }
            Text {
              text: root.statusText + (root.activeState && !root.demoMode ? " · " + CaptionModel.formatElapsed(root.elapsedSeconds) : "")
              color: Util.alpha(Color.popups.text, 0.68)
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }
          }
          Button {
            text: root.controlsExpanded ? "Less" : "More"
            foreground: Color.popups.text
            fontSize: Style.font.caption
            horizontalPadding: Style.space(7)
            verticalPadding: Style.space(5)
            tooltipText: root.controlsExpanded ? "Collapse controls" : "Expand controls"
            onClicked: root.controlsExpanded = !root.controlsExpanded
          }
          Button {
            text: "Close"
            foreground: Color.popups.text
            fontSize: Style.font.caption
            horizontalPadding: Style.space(7)
            verticalPadding: Style.space(5)
            tooltipText: "Close and stop any active capture"
            onClicked: root.dismiss()
          }
        }

        Column {
          visible: root.controlsExpanded
          width: parent.width
          spacing: Style.space(10)

          Text {
            width: parent.width
            text: root.privacyText
            color: Util.alpha(Color.popups.text, 0.72)
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          Column {
            visible: !root.demoMode && root.doctorSeen && !root.doctorReady
            width: parent.width
            spacing: Style.space(7)

            Text {
              text: "Local setup needed"
              color: Color.urgent
              font.family: Style.font.family
              font.pixelSize: Style.font.subtitle
              font.bold: true
            }
            Text {
              width: parent.width
              text: root.setupDetails
              textFormat: Text.PlainText
              color: Color.popups.text
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
            }
            Text {
              width: parent.width
              text: "The helper also discovers compatible models already downloaded by VoxType. Choose Check again after installing dependencies."
              color: Util.alpha(Color.popups.text, 0.64)
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
            Row {
              spacing: Style.space(8)

              Button {
                text: root.doctorSeen ? "Check again" : "Checking…"
                iconText: "↻"
                enabled: root.doctorSeen && !doctorProcess.running
                foreground: Color.popups.text
                bordered: true
                onClicked: root.retryDoctor()
              }
              Button {
                text: "Try demo"
                iconText: "▶"
                enabled: !doctorProcess.running && !watchProcess.running
                foreground: Color.popups.text
                accent: Color.accent
                bordered: true
                onClicked: root.open(JSON.stringify({
                  demo: true,
                  source: root.selectedSource,
                  fontScale: root.fontScale,
                  maxRows: root.maxRows,
                  position: root.captionPosition
                }))
              }
            }
          }

          Text {
            visible: !root.demoMode && !root.doctorSeen
            width: parent.width
            text: "Checking the local caption engine…"
            color: Color.popups.text
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          Column {
            visible: root.demoMode || root.doctorReady
            width: parent.width
            spacing: Style.space(8)

            Text {
              visible: !root.demoMode
              text: "Audio source"
              color: Util.alpha(Color.popups.text, 0.68)
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
              font.bold: true
            }
            Row {
              visible: !root.demoMode
              spacing: Style.space(8)
              Button {
                text: "Microphone"
                selected: root.selectedSource === "microphone"
                enabled: !root.activeState && !watchProcess.running
                foreground: Color.popups.text
                accent: Color.accent
                bordered: true
                onClicked: root.selectedSource = "microphone"
              }
              Button {
                text: "Desktop audio"
                selected: root.selectedSource === "desktop"
                enabled: !root.activeState && !watchProcess.running
                foreground: Color.popups.text
                accent: Color.accent
                bordered: true
                onClicked: root.selectedSource = "desktop"
              }
            }

            Row {
              spacing: Style.space(8)

              Button {
                visible: !root.demoMode && (root.phase === "idle" || root.phase === "error")
                text: "Start captions"
                iconText: "●"
                enabled: root.doctorReady && !watchProcess.running
                foreground: Color.popups.text
                accent: Color.accent
                bordered: true
                onClicked: root.beginCaptions()
              }
              Button {
                visible: !root.demoMode && (root.phase === "listening" || root.phase === "recording")
                text: "Pause"
                iconText: "Ⅱ"
                enabled: watchProcess.running
                foreground: Color.popups.text
                bordered: true
                onClicked: root.runAction("pause")
              }
              Button {
                visible: !root.demoMode && root.phase === "paused"
                text: "Resume"
                iconText: "▶"
                enabled: watchProcess.running
                foreground: Color.popups.text
                bordered: true
                onClicked: root.runAction("resume")
              }
              Button {
                visible: !root.demoMode && watchProcess.running
                text: root.phase === "stopping" ? "Stopping…" : "Stop"
                iconText: "■"
                enabled: watchProcess.running && root.phase !== "stopping"
                foreground: Color.urgent
                accent: Color.urgent
                bordered: true
                onClicked: root.runAction("stop")
              }
              Text {
                visible: root.demoMode
                text: "Preview mode — no audio capture"
                color: Color.popups.text
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
                font.bold: true
                anchors.verticalCenter: parent.verticalCenter
              }
            }

            Rectangle {
              visible: !root.demoMode && watchProcess.running
              width: parent.width
              height: Style.space(4)
              radius: height / 2
              color: Util.alpha(Color.popups.text, 0.16)

              Rectangle {
                width: parent.width * CaptionModel.clamp(root.inputLevel, 0, 1)
                height: parent.height
                radius: parent.radius
                color: root.statusColor
                Behavior on width { NumberAnimation { duration: 90 } }
              }
            }
          }

          Text {
            visible: root.errorMessage !== ""
            width: parent.width
            text: root.errorMessage
            textFormat: Text.PlainText
            color: Color.urgent
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          Rectangle {
            width: parent.width
            height: Math.max(1, Style.space(1))
            color: Util.alpha(Color.popups.text, 0.14)
          }

          Text {
            text: "Caption accessibility"
            color: Util.alpha(Color.popups.text, 0.68)
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            font.bold: true
          }

          RowLayout {
            width: parent.width
            spacing: Style.space(6)
            Text {
              Layout.fillWidth: true
              text: "Text size"
              color: Color.popups.text
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
            }
            Button {
              text: "A−"
              enabled: root.fontScale > 0.8
              foreground: Color.popups.text
              bordered: true
              onClicked: root.fontScale = CaptionModel.clamp(root.fontScale - 0.1, 0.8, 1.8)
            }
            Text {
              text: Math.round(root.fontScale * 100) + "%"
              color: Color.popups.text
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
            }
            Button {
              text: "A+"
              enabled: root.fontScale < 1.8
              foreground: Color.popups.text
              bordered: true
              onClicked: root.fontScale = CaptionModel.clamp(root.fontScale + 0.1, 0.8, 1.8)
            }
          }

          RowLayout {
            width: parent.width
            spacing: Style.space(6)
            Text {
              Layout.fillWidth: true
              text: "Lines shown"
              color: Color.popups.text
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
            }
            Button {
              text: "−"
              enabled: root.maxRows > 1
              foreground: Color.popups.text
              bordered: true
              onClicked: root.maxRows = Math.max(1, root.maxRows - 1)
            }
            Text {
              text: String(root.maxRows)
              color: Color.popups.text
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
            }
            Button {
              text: "+"
              enabled: root.maxRows < 5
              foreground: Color.popups.text
              bordered: true
              onClicked: root.maxRows = Math.min(5, root.maxRows + 1)
            }
          }

          RowLayout {
            width: parent.width
            spacing: Style.space(6)
            Text {
              Layout.fillWidth: true
              text: "Placement"
              color: Color.popups.text
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
            }
            Button {
              text: "Top"
              selected: root.captionPosition === "top"
              foreground: Color.popups.text
              accent: Color.accent
              bordered: true
              onClicked: root.captionPosition = "top"
            }
            Button {
              text: "Bottom"
              selected: root.captionPosition === "bottom"
              foreground: Color.popups.text
              accent: Color.accent
              bordered: true
              onClicked: root.captionPosition = "bottom"
            }
          }

          Text {
            visible: root.activeState && !root.demoMode
            width: parent.width
            text: "Final captions appear after each local inference chunk"
              + (root.lastLatencyMs > 0 ? " · estimate " + root.lastLatencyMs + " ms" : "")
            color: Util.alpha(Color.popups.text, 0.58)
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }
        }
      }
    }
  }
}
