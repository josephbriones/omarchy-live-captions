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
    session.reader = mock.Mock()
    session._state_lock = threading.Lock()
    session.transition_epoch = 0
    return session

  def test_stop_latches_and_terminates_capture_before_cleanup_waits(self) -> None:
    session = self.bare_session()

    session.request_stop()

    self.assertTrue(session.stop_event.is_set())
    self.assertEqual(
      session.capture.signal.call_args_list,
      [mock.call(signal.SIGTERM), mock.call(signal.SIGCONT)],
    )
    session.client.cancel.assert_called_once_with()
    self.assertFalse(session.paused)

  def test_pause_signals_first_then_invalidates_audio_epoch_and_queue(self) -> None:
    session = self.bare_session()
    session.capture.signal.return_value = True

    result = session.handle_control("pause")

    self.assertEqual(result, {"ok": True, "state": "paused"})
    session.capture.signal.assert_called_once_with(signal.SIGSTOP)
    session.reader.discard_pending.assert_called_once_with()
    self.assertTrue(session.paused)
    self.assertEqual(session.transition_epoch, 1)


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
    session._state_lock = threading.Lock()
    session.transition_epoch = 0

    reader = mock.Mock()
    reader.queue = queue.Queue()
    reader.overflowed = threading.Event()
    sample_count = captions.WINDOW_BYTES // captions.SAMPLE_WIDTH
    reader.queue.put(struct.pack("<h", amplitude) * sample_count)
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
