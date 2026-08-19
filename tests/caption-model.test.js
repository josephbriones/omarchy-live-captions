"use strict"

const assert = require("node:assert/strict")
const model = require("../CaptionModel.js")

let checks = 0
function test(name, body) {
  try {
    body()
    checks += 1
    process.stdout.write(`ok ${checks} - ${name}\n`)
  } catch (error) {
    process.stderr.write(`not ok ${checks + 1} - ${name}\n`)
    throw error
  }
}

test("parsePayload defaults safely and ignores autostart", () => {
  assert.deepEqual(model.parsePayload("not json"), {
    demo: false,
    source: "microphone",
    fontScale: 1,
    maxRows: 3,
    position: "bottom"
  })
  const payload = model.parsePayload('{"demo":true,"autostart":true,"source":"desktop","fontScale":99,"maxRows":0,"position":"TOP"}')
  assert.equal(payload.demo, true)
  assert.equal(payload.source, "desktop")
  assert.equal(payload.fontScale, 1.8)
  assert.equal(payload.maxRows, 1)
  assert.equal(payload.position, "top")
  assert.equal(Object.hasOwn(payload, "autostart"), false)
})

test("text normalization strips controls, folds whitespace, and bounds size", () => {
  assert.equal(model.cleanText("  hello\u0000\n   world  "), "hello world")
  assert.equal(model.cleanText("abcdef", 4), "abc…")
  assert.equal(model.cleanText(null), "")
})

test("source values stay inside the backend protocol", () => {
  assert.equal(model.sourceKey("microphone"), "microphone")
  assert.equal(model.sourceKey("desktop"), "desktop")
  assert.equal(model.sourceLabel("microphone"), "You")
  assert.equal(model.sourceLabel("desktop"), "Desktop audio")
  assert.equal(model.sourceKey("system"), "unknown")
  assert.equal(model.sourceLabel(undefined), "Speaker")
})

test("segments normalize the exact caption shape and discard empty text", () => {
  const segment = model.normalizeSegment({
    seq: 7,
    startMs: -10,
    endMs: -5,
    text: "  A caption  ",
    source: "desktop"
  }, 1)
  assert.deepEqual(segment, {
    id: "7",
    startMs: 0,
    endMs: 0,
    text: "A caption",
    source: "desktop",
    speaker: "Desktop audio"
  })
  assert.equal(model.normalizeSegment({ text: " \n " }, 2), null)
  assert.equal(model.normalizeSegment("caption", 3), null)
})

test("segment lists are bounded and visibleRows selects the tail", () => {
  const raw = Array.from({ length: 110 }, (_, index) => ({ id: index, text: `line ${index}` }))
  const normalized = model.normalizeSegments(raw)
  assert.equal(normalized.length, model.MAX_SEGMENTS)
  assert.equal(normalized[0].text, "line 14")
  assert.deepEqual(model.visibleRows(normalized, 2).map(item => item.text), ["line 108", "line 109"])
})

test("appendSegment replaces the same id and enforces its cap", () => {
  let segments = model.appendSegment([], { id: "a", text: "first", source: "microphone" }, 2)
  segments = model.appendSegment(segments, { id: "a", text: "revised", source: "microphone" }, 2)
  assert.equal(segments.length, 1)
  assert.equal(segments[0].text, "revised")
  segments = model.appendSegment(segments, { id: "b", text: "second" }, 2)
  segments = model.appendSegment(segments, { id: "c", text: "third" }, 2)
  assert.deepEqual(segments.map(item => item.id), ["b", "c"])
})

test("doctor and status events normalize the backend contract", () => {
  const doctor = model.parseEvent({
    type: "doctor",
    ready: true,
    ok: true,
    message: "Dependencies found.",
    missing: ["pw-record", "", "extra"]
  })
  assert.equal(doctor.valid, true)
  assert.equal(doctor.ready, true)
  assert.equal(doctor.message, "Dependencies found.")
  assert.deepEqual(doctor.missing, ["pw-record", "extra"])

  const status = model.parseEvent('{"type":"status","state":"stopping","source":"desktop","elapsedSeconds":4.9}')
  assert.equal(status.state, "stopping")
  assert.equal(status.source, "desktop")
  assert.equal(status.elapsedSeconds, 4)
})

test("caption events produce one normalized segment and reject blanks", () => {
  const caption = model.parseEvent({
    type: "caption",
    kind: "FINAL",
    seq: 8,
    latencyMs: 4388,
    startMs: 3000,
    endMs: 7000,
    text: " boundary safe ",
    source: "microphone"
  })
  assert.equal(caption.valid, true)
  assert.equal(caption.seq, 8)
  assert.equal(caption.latencyMs, 4388)
  assert.equal(caption.segment.id, "8")
  assert.equal(caption.segment.text, "boundary safe")
  assert.deepEqual(model.parseEvent({ type: "caption", text: "" }), { valid: false, type: "caption" })
})

test("level, heartbeat, and error events are bounded", () => {
  const level = model.parseEvent({ type: "level", source: "microphone", value: 7 })
  assert.equal(level.source, "microphone")
  assert.equal(level.value, 1)

  const heartbeat = model.parseEvent({ type: "heartbeat", state: "recording", source: "desktop", elapsedSeconds: 5 })
  assert.equal(heartbeat.state, "recording")
  assert.equal(heartbeat.source, "desktop")

  const failure = model.parseEvent({ type: "error", code: "bad\u0000code", message: "  failed\n safely " })
  assert.equal(failure.code, "badcode")
  assert.equal(failure.message, "failed safely")
})

test("unknown and malformed events cannot become valid state", () => {
  assert.deepEqual(model.parseEvent(""), { valid: false, type: "invalid" })
  assert.deepEqual(model.parseEvent("{"), { valid: false, type: "invalid" })
  assert.deepEqual(model.parseEvent("[]"), { valid: false, type: "invalid" })
  assert.deepEqual(model.parseEvent('{"type":"surprise"}'), { valid: false, type: "surprise" })
})

test("command results parse the helper's single JSON response", () => {
  const result = model.parseCommandResult('{"ok":false,"message":"not ready"}')
  assert.equal(result.valid, true)
  assert.equal(result.ok, false)
  assert.equal(result.message, "not ready")
  assert.deepEqual(model.parseCommandResult(""), {
    valid: false,
    ok: false,
    message: "Caption helper returned no JSON response."
  })
  assert.equal(model.parseCommandResult("plain diagnostic").valid, false)
  assert.equal(model.parseCommandResult("plain diagnostic").ok, false)
  assert.equal(model.parseCommandResult("plain diagnostic").message, "plain diagnostic")
  assert.equal(model.safeError('{"ok":false,"error":"model missing"}', "ignored", 2), "model missing")
  assert.equal(model.safeError("", " capture failed\n", 3), "capture failed")
  assert.equal(model.safeError("", "", 9), "Caption helper returned no JSON response.")
})

test("time and state labels stay deterministic", () => {
  assert.equal(model.formatElapsed(-1), "00:00")
  assert.equal(model.formatElapsed(65.9), "01:05")
  assert.equal(model.formatElapsed(3661), "1:01:01")
  assert.equal(model.normalizeState("mystery"), "idle")
  assert.equal(model.statusLabel("listening", false), "Listening")
  assert.equal(model.statusLabel("recording", true), "Demo")
})

process.stdout.write(`1..${checks}\n`)
