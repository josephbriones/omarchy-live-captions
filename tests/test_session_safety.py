from __future__ import annotations

import io
import json
from pathlib import Path
import queue
import signal
import struct
import sys
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import live_captions as captions  # noqa: E402


class SessionControlSafetyTests(unittest.TestCase):
  @staticmethod
  def bare_session() -> captions.CaptionSession:
    session = object.__new__(captions.CaptionSession)
    session.stop_event = threading.Event()
    session.paused = False
    session.started_at = time.monotonic()
    session.source = "microphone"
    session.writer = captions.EventWriter(io.StringIO())
    session.client = mock.Mock()
    session.capture = mock.Mock()
    session.capture.process.poll.return_value = None
    session.reader = mock.Mock()
    session._state_lock = threading.Lock()
    session.transition_epoch = 0
    session._prefetched_audio = captions.deque()
    return session

  def test_stop_latches_and_terminates_capture_before_cleanup_waits(self) -> None:
    session = self.bare_session()

    session.request_stop()

    self.assertTrue(session.stop_event.is_set())
    session.capture.signal.assert_called_once_with(signal.SIGTERM)
    session.client.cancel.assert_called_once_with()
    self.assertFalse(session.paused)

  def test_pause_keeps_capture_draining_and_invalidates_audio_epoch_and_queue(self) -> None:
    session = self.bare_session()

    result = session.handle_control("pause")

    self.assertEqual(result, {"ok": True, "state": "paused"})
    session.capture.signal.assert_not_called()
    session.reader.discard_pending.assert_called_once_with()
    self.assertTrue(session.paused)
    self.assertEqual(session.transition_epoch, 1)

  def test_pause_and_resume_are_idempotent(self) -> None:
    session = self.bare_session()

    self.assertEqual(session.handle_control("resume"), {"ok": True, "state": "listening"})
    self.assertEqual(session.handle_control("pause"), {"ok": True, "state": "paused"})
    self.assertEqual(session.handle_control("pause"), {"ok": True, "state": "paused"})
    self.assertEqual(session.handle_control("resume"), {"ok": True, "state": "listening"})
    self.assertEqual(session.handle_control("resume"), {"ok": True, "state": "listening"})

    session.capture.signal.assert_not_called()
    self.assertEqual(session.transition_epoch, 2)
    events = [json.loads(line) for line in session.writer.stream.getvalue().splitlines()]
    self.assertEqual([event["state"] for event in events], ["paused", "listening"])

  def test_no_stale_listening_heartbeat_can_follow_paused_status(self) -> None:
    heartbeat_started = threading.Event()
    release_heartbeat = threading.Event()

    class BlockingWriter(captions.EventWriter):
      def emit(self, event: dict[str, object]) -> bool:
        if event.get("type") == "heartbeat":
          heartbeat_started.set()
          release_heartbeat.wait(1.0)
        return super().emit(event)

    session = self.bare_session()
    output = io.StringIO()
    session.writer = BlockingWriter(output)
    heartbeat_thread = threading.Thread(
      target=session._emit_heartbeat,
      args=(time.monotonic(),),
    )
    pause_thread = threading.Thread(target=session.handle_control, args=("pause",))

    heartbeat_thread.start()
    self.assertTrue(heartbeat_started.wait(1.0))
    pause_thread.start()
    pause_thread.join(0.05)
    self.assertTrue(pause_thread.is_alive(), "pause crossed an in-flight heartbeat boundary")
    release_heartbeat.set()
    heartbeat_thread.join(1.0)
    pause_thread.join(1.0)

    self.assertFalse(heartbeat_thread.is_alive())
    self.assertFalse(pause_thread.is_alive())
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    paused_index = next(index for index, event in enumerate(events) if event.get("state") == "paused")
    self.assertFalse(any(event.get("state") == "listening" for event in events[paused_index + 1 :]))


class SessionRuntimeSafetyTests(unittest.TestCase):
  @staticmethod
  def bare_runtime_session() -> captions.CaptionSession:
    session = SessionControlSafetyTests.bare_session()
    session.language = "en"
    session.server = None
    session.capture.process.poll.return_value = None
    session.capture.stderr_drain = None
    reader = mock.Mock()
    reader.queue = queue.Queue()
    reader.overflowed = threading.Event()
    session.reader = reader
    return session

  def test_capture_readiness_requires_first_pcm_with_a_bounded_timeout(self) -> None:
    session = self.bare_runtime_session()
    with self.assertRaises(captions.CaptionError) as caught:
      session.wait_until_capture_ready(timeout=0.01)
    self.assertEqual(caught.exception.code, "capture-no-audio")

    chunk = captions.CapturedChunk(b"pcm", 0, time.monotonic())
    session.reader.queue.put(chunk)
    session.wait_until_capture_ready(timeout=0.1)
    self.assertEqual(session._prefetched_audio.popleft(), chunk)

  def test_caption_loop_supervises_server_and_rejects_stale_audio(self) -> None:
    session = self.bare_runtime_session()
    session.server = mock.Mock()
    session.server.process.poll.return_value = 9
    session.server.stderr_drain = None
    with self.assertRaises(captions.CaptionError) as server_exit:
      session.run_caption_loop()
    self.assertEqual(server_exit.exception.code, "server-exited")

    session.server = None
    session.reader.queue.put(
      captions.CapturedChunk(
        b"pcm",
        0,
        time.monotonic() - captions.MAX_CAPTURE_BACKLOG_SECONDS - 1.0,
      )
    )
    with self.assertRaises(captions.CaptionError) as backlog:
      session.run_caption_loop()
    self.assertEqual(backlog.exception.code, "capture-backlog")
    self.assertIn("smaller model", backlog.exception.message)

    session.reader.overflowed.set()
    with self.assertRaises(captions.CaptionError) as overflow:
      session.run_caption_loop()
    self.assertEqual(overflow.exception.code, "capture-backlog")

  def test_unexpected_capture_eof_is_an_error_even_after_exit_zero(self) -> None:
    session = self.bare_runtime_session()
    session.capture.process.poll.return_value = 0
    session.reader.queue.put(None)

    with self.assertRaises(captions.CaptionError) as caught:
      session.run_caption_loop()

    self.assertEqual(caught.exception.code, "capture-exited")
    self.assertIn("unexpectedly", caught.exception.message)

  def test_pause_status_is_a_strict_boundary_for_caption_events(self) -> None:
    caption_started = threading.Event()
    release_caption = threading.Event()

    class BlockingWriter(captions.EventWriter):
      def emit(self, event: dict[str, object]) -> bool:
        if event.get("type") == "caption":
          caption_started.set()
          release_caption.wait(1.0)
        return super().emit(event)

    session = self.bare_runtime_session()
    output = io.StringIO()
    session.writer = BlockingWriter(output)
    session.client.transcribe.return_value = "caption before pause"
    samples = captions.WINDOW_BYTES // captions.SAMPLE_WIDTH
    session.reader.queue.put(
      captions.CapturedChunk(struct.pack("<h", 1000) * samples, 0, time.monotonic())
    )
    errors: list[BaseException] = []

    def run_loop() -> None:
      try:
        session.run_caption_loop()
      except BaseException as error:
        errors.append(error)

    def pause() -> None:
      try:
        session.handle_control("pause")
      finally:
        session.stop_event.set()

    loop_thread = threading.Thread(target=run_loop)
    loop_thread.start()
    self.assertTrue(caption_started.wait(1.0))
    pause_thread = threading.Thread(target=pause)
    pause_thread.start()
    pause_thread.join(0.05)
    self.assertTrue(pause_thread.is_alive(), "pause did not wait for the in-flight caption boundary")
    release_caption.set()
    pause_thread.join(1.0)
    loop_thread.join(1.0)
    self.assertFalse(pause_thread.is_alive())
    self.assertFalse(loop_thread.is_alive())
    self.assertEqual(errors, [])
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    paused_index = next(index for index, event in enumerate(events) if event.get("state") == "paused")
    self.assertFalse(any(event.get("type") == "caption" for event in events[paused_index + 1 :]))


class SilenceGateTests(unittest.TestCase):
  @staticmethod
  def run_window(amplitude: int) -> tuple[mock.Mock, list[dict[str, object]]]:
    session = object.__new__(captions.CaptionSession)
    session.stop_event = threading.Event()
    session.paused = False
    session.started_at = time.monotonic()
    session.source = "microphone"
    session.language = "en"
    stream = io.StringIO()
    session.writer = captions.EventWriter(stream)
    session.client = mock.Mock()
    session.client.transcribe.return_value = "threshold speech"
    session.capture = mock.Mock()
    session.capture.process.poll.return_value = 0
    session.capture.stderr_drain = None
    session.server = None
    session._state_lock = threading.Lock()
    session.transition_epoch = 0
    session._prefetched_audio = captions.deque()

    class StopAtEofQueue(queue.Queue):
      def get(self, *args: object, **kwargs: object) -> object:
        value = super().get(*args, **kwargs)
        if value is None:
          session.stop_event.set()
        return value

    reader = mock.Mock()
    reader.queue = StopAtEofQueue()
    reader.overflowed = threading.Event()
    sample_count = captions.WINDOW_BYTES // captions.SAMPLE_WIDTH
    reader.queue.put(
      captions.CapturedChunk(
        struct.pack("<h", amplitude) * sample_count,
        0,
        time.monotonic(),
      )
    )
    reader.queue.put(None)
    session.reader = reader

    result = captions.CaptionSession.run_caption_loop(session)
    if result != 0:
      raise AssertionError(f"caption loop returned {result}")
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    return session.client, events

  def test_silence_gate_skips_only_below_threshold_window(self) -> None:
    boundary = captions.SILENCE_RMS_THRESHOLD * 32768.0
    below_client, below_events = self.run_window(max(0, int(boundary) - 1))
    above_client, above_events = self.run_window(int(boundary) + 1)

    below_client.transcribe.assert_not_called()
    self.assertFalse(any(event.get("type") == "caption" for event in below_events))
    above_client.transcribe.assert_called_once()
    self.assertTrue(any(event.get("type") == "caption" for event in above_events))


if __name__ == "__main__":
  unittest.main()
