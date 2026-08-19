// Pure state-normalization helpers shared by QML and the Node test suite.
// Keep this file free of QML globals so malformed helper output can never
// crash or grow the long-lived overlay model without bounds.

var MAX_SEGMENTS = 96
var MAX_TEXT_LENGTH = 1200

function clamp(value, minimum, maximum) {
  var number = Number(value)
  if (!isFinite(number)) number = minimum
  return Math.max(minimum, Math.min(maximum, number))
}

function cleanText(value, maximum) {
  var limit = maximum === undefined ? MAX_TEXT_LENGTH : Math.max(0, Number(maximum) || 0)
  var text = String(value === undefined || value === null ? "" : value)
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "")
    .replace(/\s+/g, " ")
    .trim()
  if (text.length > limit) return text.slice(0, Math.max(0, limit - 1)) + "…"
  return text
}

function parseBoolean(value, fallback) {
  return typeof value === "boolean" ? value : fallback
}

function plainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value)
}

function parseJson(raw) {
  if (plainObject(raw)) return raw
  if (typeof raw !== "string" || raw.trim() === "") return {}
  try {
    var parsed = JSON.parse(raw)
    return plainObject(parsed) ? parsed : {}
  } catch (error) {
    return {}
  }
}

function parsePayload(raw) {
  var payload = parseJson(raw)
  var position = String(payload.position || "bottom").toLowerCase()
  if (position !== "top") position = "bottom"

  return {
    // Demo mode is synthetic and safe to activate when opening. There is no
    // autostart option: real audio always needs an explicit Start action.
    demo: parseBoolean(payload.demo, false),
    source: sourceKey(payload.source) === "desktop" ? "desktop" : "microphone",
    fontScale: clamp(payload.fontScale === undefined ? 1 : payload.fontScale, 0.8, 1.8),
    maxRows: Math.round(clamp(payload.maxRows === undefined ? 3 : payload.maxRows, 1, 5)),
    position: position
  }
}

function sourceKey(value) {
  var source = cleanText(value, 40).toLowerCase()
  return source === "microphone" || source === "desktop" ? source : "unknown"
}

function sourceLabel(value) {
  var source = sourceKey(value)
  if (source === "microphone") return "You"
  if (source === "desktop") return "Desktop audio"
  return source === "unknown" ? "Speaker" : cleanText(source, 40)
}

function normalizeSegment(segment, fallbackIndex) {
  if (!plainObject(segment)) return null
  var text = cleanText(segment.text, MAX_TEXT_LENGTH)
  if (text === "") return null

  var startMs = Math.max(0, Number(segment.startMs) || 0)
  var endMs = Math.max(startMs, Number(segment.endMs) || startMs)
  var source = sourceKey(segment.source)

  return {
    id: cleanText(segment.id === undefined ? segment.seq : segment.id, 100)
      || String(fallbackIndex === undefined ? 0 : fallbackIndex),
    startMs: startMs,
    endMs: endMs,
    text: text,
    source: source,
    speaker: sourceLabel(source)
  }
}

function normalizeSegments(segments, cap) {
  if (!Array.isArray(segments)) return []
  var maximum = Math.round(clamp(cap === undefined ? MAX_SEGMENTS : cap, 1, MAX_SEGMENTS))
  var result = []
  for (var index = 0; index < segments.length; index++) {
    var segment = normalizeSegment(segments[index], index)
    if (segment) result.push(segment)
  }
  if (result.length > maximum) result = result.slice(result.length - maximum)
  return result
}

function visibleRows(segments, count) {
  var normalized = normalizeSegments(segments, MAX_SEGMENTS)
  var maximum = Math.round(clamp(count === undefined ? 3 : count, 1, 5))
  return normalized.slice(Math.max(0, normalized.length - maximum))
}

function appendSegment(segments, segment, cap) {
  var current = normalizeSegments(segments, MAX_SEGMENTS)
  var next = normalizeSegment(segment, current.length)
  if (!next) return current
  var replaced = false
  for (var index = 0; index < current.length; index++) {
    if (current[index].id !== next.id) continue
    current[index] = next
    replaced = true
    break
  }
  if (!replaced) current.push(next)
  var maximum = Math.round(clamp(cap === undefined ? MAX_SEGMENTS : cap, 1, MAX_SEGMENTS))
  return current.slice(Math.max(0, current.length - maximum))
}

function speakerLabel(segment) {
  if (!plainObject(segment)) return "Speaker"
  return cleanText(segment.speaker, 60) || sourceLabel(segment.source)
}

function normalizeState(value) {
  var state = String(value || "idle").toLowerCase()
  if (["idle", "checking", "setup", "starting", "listening", "recording", "paused", "stopping", "error"].indexOf(state) === -1)
    return "idle"
  return state
}

function eventBase(type) {
  return { valid: true, type: type }
}

function parseEvent(raw) {
  var data
  if (plainObject(raw)) data = raw
  else {
    if (typeof raw !== "string" || raw.trim() === "") return { valid: false, type: "invalid" }
    try { data = JSON.parse(raw) }
    catch (error) { return { valid: false, type: "invalid" } }
  }
  if (!plainObject(data)) return { valid: false, type: "invalid" }

  var type = String(data.type || "").toLowerCase()
  if (type === "doctor") {
    var doctor = eventBase("doctor")
    doctor.ready = parseBoolean(data.ready, false)
    doctor.ok = parseBoolean(data.ok, doctor.ready)
    doctor.message = cleanText(data.message, 400)
    doctor.missing = Array.isArray(data.missing)
      ? data.missing.slice(0, 8).map(function(item) { return cleanText(item, 100) }).filter(Boolean)
      : []
    return doctor
  }

  if (type === "status") {
    var status = eventBase("status")
    status.state = normalizeState(data.state)
    status.source = sourceKey(data.source)
    status.elapsedSeconds = Math.max(0, Math.floor(Number(data.elapsedSeconds) || 0))
    return status
  }


  if (type === "caption") {
    var caption = eventBase("caption")
    caption.kind = cleanText(data.kind || "final", 20).toLowerCase()
    caption.seq = Math.max(0, Math.floor(Number(data.seq) || 0))
    caption.latencyMs = Math.max(0, Math.floor(Number(data.latencyMs) || 0))
    caption.segment = normalizeSegment({
      id: data.id === undefined ? data.seq : data.id,
      seq: data.seq,
      startMs: data.startMs,
      endMs: data.endMs,
      text: data.text,
      source: data.source
    }, caption.seq)
    if (!caption.segment) return { valid: false, type: "caption" }
    return caption
  }

  if (type === "level") {
    var level = eventBase("level")
    level.source = sourceKey(data.source)
    level.value = clamp(data.value, 0, 1)
    return level
  }

  if (type === "error") {
    var failure = eventBase("error")
    failure.code = cleanText(data.code, 80)
    failure.message = cleanText(data.message || data.error || "Live captions encountered an error.", 500)
    return failure
  }

  if (type === "heartbeat") {
    var heartbeat = eventBase("heartbeat")
    heartbeat.state = data.state === undefined ? "" : normalizeState(data.state)
    heartbeat.source = sourceKey(data.source)
    heartbeat.elapsedSeconds = Math.max(0, Math.floor(Number(data.elapsedSeconds) || 0))
    return heartbeat
  }

  return { valid: false, type: type || "invalid" }
}

function parseCommandResult(raw) {
  var text = String(raw || "").trim()
  if (text === "") {
    return {
      valid: false,
      ok: false,
      message: "Caption helper returned no JSON response."
    }
  }
  try {
    var parsed = JSON.parse(text)
    if (plainObject(parsed)) {
      return {
        valid: true,
        ok: parseBoolean(parsed.ok, !parsed.error),
        message: cleanText(parsed.message || parsed.error, 500),
        data: parsed
      }
    }
  } catch (error) {}
  return {
    valid: false,
    ok: false,
    message: cleanText(text, 500) || "Caption helper returned invalid JSON."
  }
}

function safeError(stdout, stderr, exitCode) {
  var result = parseCommandResult(stdout)
  if (result.valid && result.message && (!result.ok || Number(exitCode) !== 0)) return result.message
  var diagnostic = cleanText(stderr, 500)
  if (diagnostic) return diagnostic
  if (!result.valid && result.message) return result.message
  return "Caption helper exited with code " + String(exitCode === undefined ? "?" : exitCode) + "."
}

function formatElapsed(seconds) {
  var total = Math.max(0, Math.floor(Number(seconds) || 0))
  var hours = Math.floor(total / 3600)
  var minutes = Math.floor((total % 3600) / 60)
  var remainder = total % 60
  function two(value) { return value < 10 ? "0" + value : String(value) }
  return hours > 0 ? hours + ":" + two(minutes) + ":" + two(remainder) : two(minutes) + ":" + two(remainder)
}

function statusLabel(state, demo) {
  if (demo) return "Demo"
  var normalized = normalizeState(state)
  if (normalized === "checking") return "Checking setup"
  if (normalized === "setup") return "Setup needed"
  if (normalized === "starting") return "Starting"
  if (normalized === "listening") return "Listening"
  if (normalized === "recording") return "Captioning"
  if (normalized === "paused") return "Paused"
  if (normalized === "stopping") return "Stopping"
  if (normalized === "error") return "Needs attention"
  return "Ready"
}

if (typeof module !== "undefined") {
  module.exports = {
    MAX_SEGMENTS: MAX_SEGMENTS,
    MAX_TEXT_LENGTH: MAX_TEXT_LENGTH,
    clamp: clamp,
    cleanText: cleanText,
    parsePayload: parsePayload,
    sourceKey: sourceKey,
    sourceLabel: sourceLabel,
    normalizeSegment: normalizeSegment,
    normalizeSegments: normalizeSegments,
    visibleRows: visibleRows,
    appendSegment: appendSegment,
    speakerLabel: speakerLabel,
    normalizeState: normalizeState,
    parseEvent: parseEvent,
    parseCommandResult: parseCommandResult,
    safeError: safeError,
    formatElapsed: formatElapsed,
    statusLabel: statusLabel
  }
}
