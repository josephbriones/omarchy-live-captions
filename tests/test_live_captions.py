from __future__ import annotations

from contextlib import contextmanager
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import queue
import signal
import socket
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock
import wave


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import live_captions as captions  # noqa: E402


class _WhisperHandler(BaseHTTPRequestHandler):
  protocol_version = "HTTP/1.1"

  def log_message(self, _format: str, *_args: object) -> None:
    return

  def _send(self, status: int, payload: bytes, content_type: str = "application/json") -> None:
    self.send_response(status)
    self.send_header("Content-Type", content_type)
    self.send_header("Content-Length", str(len(payload)))
    self.send_header("Connection", "close")
    self.end_headers()
    try:
      self.wfile.write(payload)
    except BrokenPipeError:
      pass

  def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
    owner = self.server
    owner.requests.append(("GET", self.path, dict(self.headers), b""))
    self._send(owner.status, owner.response_body)

  def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
    size = int(self.headers.get("Content-Length", "0"))
    body = self.rfile.read(size)
    owner = self.server
    owner.requests.append(("POST", self.path, dict(self.headers), body))
    self._send(owner.status, owner.response_body)


class _WhisperServer(ThreadingHTTPServer):
  daemon_threads = True

  def __init__(self) -> None:
    super().__init__(("127.0.0.1", 0), _WhisperHandler)
    self.status = 200
    self.response_body = b'{"status":"ok"}'
    self.requests: list[tuple[str, str, dict[str, str], bytes]] = []


@contextmanager
def whisper_server():
  server = _WhisperServer()
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  try:
    yield server
  finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2.0)


class ConfigTests(unittest.TestCase):
  def test_config_path_honors_xdg_without_touching_home(self) -> None:
    result = captions.config_path({"XDG_CONFIG_HOME": "/tmp/example-config", "HOME": "/ignored"})
    self.assertEqual(result, Path("/tmp/example-config/omarchy/live-captions/config.json"))

  def test_runtime_path_requires_an_absolute_user_owned_runtime_directory(self) -> None:
    with tempfile.TemporaryDirectory() as temporary:
      base = Path(temporary)
      self.assertEqual(
        captions.runtime_dir({"XDG_RUNTIME_DIR": str(base)}),
        base / "omarchy-live-captions",
      )
      with self.assertRaises(captions.CaptionError) as relative:
        captions.runtime_dir({"XDG_RUNTIME_DIR": "relative/runtime"})
      self.assertEqual(relative.exception.code, "unsafe-runtime")

      target = base / "target"
      target.mkdir()
      link = base / "runtime-link"
      link.symlink_to(target, target_is_directory=True)
      with self.assertRaises(captions.CaptionError) as symlink:
        captions.runtime_dir({"XDG_RUNTIME_DIR": str(link)})
      self.assertEqual(symlink.exception.code, "unsafe-runtime")

  def test_atomic_config_is_private_and_round_trips(self) -> None:
    with tempfile.TemporaryDirectory() as temporary:
      path = Path(temporary) / "nested" / "config.json"
      value = {
        "schemaVersion": 1,
        "model": "/models/ggml-base.en.bin",
        "source": "desktop",
        "language": "pt-BR",
      }
      captions.atomic_write_json(path, value)

      self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
      self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
      self.assertEqual(json.loads(path.read_text(encoding="utf-8")), value)
      self.assertEqual(captions.load_config(path), value)

      os.chmod(path, 0o666)
      captions.atomic_write_json(path, value | {"source": "microphone"})
      self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

  def test_private_directory_rejects_symlink(self) -> None:
    with tempfile.TemporaryDirectory() as temporary:
      root = Path(temporary)
      target = root / "target"
      target.mkdir()
      link = root / "link"
      link.symlink_to(target, target_is_directory=True)
      with self.assertRaisesRegex(captions.CaptionError, "symlinked private directory") as caught:
        captions.ensure_private_directory(link)
      self.assertEqual(caught.exception.code, "unsafe-directory")

  def test_load_config_rejects_invalid_schema_and_values(self) -> None:
    invalid_values = (
      [],
      {"schemaVersion": 2},
      {"schemaVersion": 1, "source": "network"},
      {"schemaVersion": 1, "language": "en\r\nInjected"},
    )
    with tempfile.TemporaryDirectory() as temporary:
      path = Path(temporary) / "config.json"
      for value in invalid_values:
        with self.subTest(value=value):
          path.write_text(json.dumps(value), encoding="utf-8")
          with self.assertRaises(captions.CaptionError):
            captions.load_config(path)


class ModelTests(unittest.TestCase):
  def test_checked_model_requires_absolute_readable_safe_bin(self) -> None:
    with tempfile.TemporaryDirectory() as temporary:
      model = Path(temporary) / "ggml-base.en.bin"
      model.write_bytes(b"local model fixture")
      os.chmod(model, 0o600)
      self.assertEqual(captions.checked_model_path(model), model.resolve())

      with self.assertRaisesRegex(captions.CaptionError, "absolute") as relative:
        captions.checked_model_path("ggml-base.en.bin")
      self.assertEqual(relative.exception.code, "invalid-model")

      wrong_suffix = Path(temporary) / "model.gguf"
      wrong_suffix.write_bytes(b"fixture")
      with self.assertRaises(captions.CaptionError) as suffix:
        captions.checked_model_path(wrong_suffix)
      self.assertEqual(suffix.exception.code, "invalid-model")

      os.chmod(model, 0o606)
      with self.assertRaises(captions.CaptionError) as writable:
        captions.checked_model_path(model)
      self.assertEqual(writable.exception.code, "unsafe-model")

  def test_checked_model_rejects_missing_and_directory(self) -> None:
    with tempfile.TemporaryDirectory() as temporary:
      missing = Path(temporary) / "missing.bin"
      with self.assertRaises(captions.CaptionError) as absent:
        captions.checked_model_path(missing)
      self.assertEqual(absent.exception.code, "model-missing")

      directory = Path(temporary) / "directory.bin"
      directory.mkdir()
      with self.assertRaises(captions.CaptionError) as not_file:
        captions.checked_model_path(directory)
      self.assertEqual(not_file.exception.code, "invalid-model")

  def test_discovery_precedence_and_invalid_explicit_does_not_fall_back(self) -> None:
    with tempfile.TemporaryDirectory() as temporary:
      root = Path(temporary)
      environment_model = root / "environment.bin"
      configured_model = root / "configured.bin"
      for path in (environment_model, configured_model):
        path.write_bytes(b"fixture")
        os.chmod(path, 0o600)
      env = {"HOME": temporary, "LIVE_CAPTIONS_MODEL": str(environment_model)}
      found, origin, issue = captions.discover_model(
        config={"model": str(configured_model)}, environ=env
      )
      self.assertEqual((found, origin, issue), (environment_model.resolve(), "environment", ""))

      found, origin, issue = captions.discover_model(
        root / "missing.bin", config={"model": str(configured_model)}, environ=env
      )
      self.assertIsNone(found)
      self.assertEqual(origin, "argument")
      self.assertIn("unavailable", issue)


class DoctorCapabilityTests(unittest.TestCase):
  def test_whisper_help_probe_requires_every_fixed_server_option(self) -> None:
    complete_help = " ".join(captions.REQUIRED_WHISPER_SERVER_FLAGS).encode("utf-8")
    with mock.patch.object(
      captions.subprocess,
      "run",
      return_value=captions.subprocess.CompletedProcess([], 0, b"", complete_help),
    ):
      self.assertEqual(captions.whisper_server_capability_issue("/usr/bin/whisper-server"), "")

    with mock.patch.object(
      captions.subprocess,
      "run",
      return_value=captions.subprocess.CompletedProcess([], 0, b"", b"--model --host --port"),
    ):
      issue = captions.whisper_server_capability_issue("/usr/bin/whisper-server")
    self.assertIn("--request-path", issue)
    self.assertIn("--public", issue)


class AudioAndProtocolTests(unittest.TestCase):
  def test_pcm_to_wav_is_mono_signed_16_bit_16khz_and_trims_partial_sample(self) -> None:
    pcm = b"\x01\x02\x03\x04\xff"
    payload = captions.pcm_to_wav(pcm)
    with wave.open(io.BytesIO(payload), "rb") as wav:
      self.assertEqual(wav.getnchannels(), 1)
      self.assertEqual(wav.getsampwidth(), 2)
      self.assertEqual(wav.getframerate(), 16_000)
      self.assertEqual(wav.getnframes(), 2)
      self.assertEqual(wav.readframes(2), pcm[:4])

  def test_pcm_level_handles_silence_full_scale_and_partial_sample(self) -> None:
    self.assertEqual(captions.pcm_level(b"\0" * 8), 0.0)
    self.assertAlmostEqual(captions.pcm_level(b"\xff\x7f" * 8), 32767 / 32768, places=4)
    self.assertEqual(captions.pcm_level(b"\xff"), 0.0)

  def test_multipart_contains_wav_and_required_fields(self) -> None:
    wav_bytes = b"RIFF\x00fixture"
    with mock.patch.object(captions.secrets, "token_hex", return_value="a" * 32):
      body, boundary = captions.multipart_inference_body(wav_bytes, "pt-BR")
    self.assertEqual(boundary, "live-captions-" + "a" * 32)
    self.assertTrue(body.startswith(("--" + boundary + "\r\n").encode("ascii")))
    self.assertTrue(body.endswith(("--" + boundary + "--\r\n").encode("ascii")))
    self.assertIn(b'name="file"; filename="chunk.wav"', body)
    self.assertIn(b"Content-Type: audio/wav\r\n\r\n" + wav_bytes + b"\r\n", body)
    self.assertIn(b'name="response_format"\r\n\r\njson\r\n', body)
    self.assertIn(b'name="language"\r\n\r\npt-BR\r\n', body)

  def test_transcript_deduper_removes_exact_normalized_overlap(self) -> None:
    deduper = captions.TranscriptDeduper()
    self.assertEqual(deduper.novel_text("Hello, brave world!"), "Hello, brave world!")
    self.assertEqual(deduper.novel_text("BRAVE world, and beyond."), "and beyond.")
    self.assertEqual(deduper.novel_text("and beyond"), "")
    self.assertEqual(deduper.history[-5:], ("hello", "brave", "world", "and", "beyond"))

  def test_transcript_deduper_handles_repetitive_ambiguous_boundaries_deterministically(self) -> None:
    deduper = captions.TranscriptDeduper()
    self.assertEqual(deduper.novel_text("go go now"), "go go now")
    self.assertEqual(deduper.novel_text("go now go go now"), "go go now")
    self.assertEqual(deduper.novel_text("go now later"), "later")
    self.assertEqual(deduper.novel_text("later later"), "later")

  def test_capture_command_is_fixed_raw_pcm_and_source_scoped(self) -> None:
    microphone = captions.capture_command("/usr/bin/pw-record", "microphone")
    desktop = captions.capture_command("/usr/bin/pw-record", "desktop")
    required = {
      "--raw",
      "--rate=16000",
      "--channels=1",
      "--channel-map=mono",
      "--format=s16",
      "--latency=50ms",
    }
    self.assertTrue(required.issubset(microphone), microphone)
    self.assertEqual(microphone[-1], "-")
    self.assertFalse(any("stream.capture.sink" in item for item in microphone))
    self.assertIn('--properties={"stream.capture.sink":true}', desktop)
    self.assertEqual(desktop[-1], "-")


class LocalWhisperClientTests(unittest.TestCase):
  TOKEN = "0123456789abcdef0123456789abcdef"

  def test_rejects_invalid_request_token(self) -> None:
    with self.assertRaises(captions.CaptionError) as caught:
      captions.LocalWhisperClient(1, "../inference")
    self.assertEqual(caught.exception.code, "invalid-token")

  def test_health_and_inference_use_hard_coded_loopback_and_private_prefix(self) -> None:
    with whisper_server() as server:
      client = captions.LocalWhisperClient(server.server_port, self.TOKEN, timeout=1.0)
      with mock.patch.dict(
        os.environ,
        {"HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""},
      ):
        self.assertTrue(client.healthy())
        server.response_body = b'{"text":"  local   words \\u0001 "}'
        self.assertEqual(client.transcribe(b"RIFFfixture", "en"), "local words")

      self.assertEqual(server.requests[0][0:2], ("GET", f"/{self.TOKEN}/health"))
      method, path, headers, body = server.requests[1]
      self.assertEqual((method, path), ("POST", f"/{self.TOKEN}/inference"))
      self.assertIn("multipart/form-data; boundary=live-captions-", headers["Content-Type"])
      self.assertIn(b"RIFFfixture", body)
      self.assertIn(b'name="language"\r\n\r\nen\r\n', body)

  def test_health_fails_closed_for_bad_status_schema_and_json(self) -> None:
    with whisper_server() as server:
      client = captions.LocalWhisperClient(server.server_port, self.TOKEN, timeout=1.0)
      for status, payload in (
        (503, b'{"status":"ok"}'),
        (200, b'{"status":"loading"}'),
        (200, b"not-json"),
      ):
        with self.subTest(status=status, payload=payload):
          server.status = status
          server.response_body = payload
          self.assertFalse(client.healthy())

  def test_inference_maps_http_json_schema_and_size_errors(self) -> None:
    cases = (
      (503, b'{"text":"no"}', "inference-http"),
      (200, b"not-json", "inference-json"),
      (200, b'{"status":"ok"}', "inference-schema"),
      (200, b"x" * (captions.MAX_HTTP_RESPONSE + 1), "inference-response"),
    )
    with whisper_server() as server:
      client = captions.LocalWhisperClient(server.server_port, self.TOKEN, timeout=1.0)
      for status, payload, code in cases:
        with self.subTest(code=code):
          server.status = status
          server.response_body = payload
          with self.assertRaises(captions.CaptionError) as caught:
            client.transcribe(b"RIFFfixture", "en")
          self.assertEqual(caught.exception.code, code)

  def test_inference_unavailable_is_a_stable_user_error(self) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    client = captions.LocalWhisperClient(port, self.TOKEN, timeout=0.1)
    with self.assertRaises(captions.CaptionError) as caught:
      client.transcribe(b"RIFFfixture", "en")
    self.assertEqual(caught.exception.code, "inference-unavailable")

  def test_cancel_just_before_inference_opens_is_latched(self) -> None:
    constructed = threading.Event()
    release = threading.Event()

    class DeferredConnection:
      def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.requested = False
        constructed.set()
        release.wait(1.0)

      def request(self, *_args: object, **_kwargs: object) -> None:
        self.requested = True

      def close(self) -> None:
        return

    client = captions.LocalWhisperClient(43008, self.TOKEN, timeout=1.0)
    errors: list[captions.CaptionError] = []
    connections: list[DeferredConnection] = []

    def connection(*args: object, **kwargs: object) -> DeferredConnection:
      item = DeferredConnection(*args, **kwargs)
      connections.append(item)
      return item

    def transcribe() -> None:
      try:
        client.transcribe(b"RIFFfixture", "en")
      except captions.CaptionError as error:
        errors.append(error)

    with mock.patch.object(captions.http.client, "HTTPConnection", side_effect=connection):
      worker = threading.Thread(target=transcribe)
      worker.start()
      self.assertTrue(constructed.wait(1.0))
      client.cancel()
      release.set()
      worker.join(1.0)

    self.assertFalse(worker.is_alive())
    self.assertEqual([error.code for error in errors], ["stopped"])
    self.assertEqual(len(connections), 1)
    self.assertFalse(connections[0].requested)


class CaptureReaderTests(unittest.TestCase):
  def test_queue_overflow_drops_oldest_without_blocking(self) -> None:
    reader = captions.CaptureReader(io.BytesIO(), threading.Event())
    reader.queue = queue.Queue(maxsize=2)
    reader._put(b"oldest")
    reader._put(b"middle")

    producer = threading.Thread(target=reader._put, args=(b"newest",), daemon=True)
    producer.start()
    producer.join(timeout=0.5)
    self.assertFalse(producer.is_alive(), "capture producer blocked behind inference")
    self.assertTrue(reader.overflowed.is_set())
    self.assertEqual(reader.queue.get_nowait(), b"middle")
    self.assertEqual(reader.queue.get_nowait(), b"newest")

  def test_reader_emits_eof_sentinel_and_can_discard_pending(self) -> None:
    reader = captions.CaptureReader(io.BytesIO(b"audio"), threading.Event())
    reader.start()
    reader.thread.join(timeout=1.0)
    self.assertFalse(reader.thread.is_alive())
    self.assertEqual(reader.queue.get_nowait(), b"audio")
    self.assertIsNone(reader.queue.get_nowait())
    reader.queue.put_nowait(b"stale")
    reader.discard_pending()
    self.assertTrue(reader.queue.empty())


class ControlReaderTests(unittest.TestCase):
  def test_stdin_control_dispatches_only_known_commands(self) -> None:
    seen: list[str] = []
    reader = captions.ControlReader(
      io.StringIO("pause\nunknown\nresume\nstop\n"),
      lambda command: seen.append(command) or {"ok": True},
      captions.EventWriter(io.StringIO()),
    )
    reader.start()
    reader.thread.join(timeout=1.0)
    self.assertFalse(reader.thread.is_alive())
    self.assertEqual(seen, ["pause", "resume"])

  def test_stdin_control_surfaces_actionable_failures(self) -> None:
    output = io.StringIO()

    def fail(_command: str) -> dict[str, object]:
      raise captions.CaptionError("not-paused", "The caption session is not paused.")

    reader = captions.ControlReader(
      io.StringIO("resume\n"),
      fail,
      captions.EventWriter(output),
    )
    reader.start()
    reader.thread.join(timeout=1.0)
    event = json.loads(output.getvalue())
    self.assertEqual((event["type"], event["code"]), ("error", "not-paused"))


class DemoAndCliTests(unittest.TestCase):
  def test_demo_is_dependency_free_jsonl_with_expected_lifecycle(self) -> None:
    output = io.StringIO()
    code = captions.run_demo(captions.EventWriter(output), interval=0.0)
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    self.assertEqual(code, 0)
    self.assertEqual([event["state"] for event in events[:2]], ["starting", "listening"])
    caption_events = [event for event in events if event["type"] == "caption"]
    self.assertEqual(len(caption_events), len(captions.DEMO_CAPTIONS))
    self.assertEqual([event["seq"] for event in caption_events], list(range(len(captions.DEMO_CAPTIONS))))
    self.assertTrue(all(event["kind"] == "final" and event["text"] for event in caption_events))
    self.assertFalse(any(event["type"] == "error" for event in events))

  def test_parser_exposes_safe_subcommands_and_flags(self) -> None:
    parser = captions.build_parser()
    watch = parser.parse_args(["watch", "--demo", "--source", "desktop", "--language", "auto"])
    self.assertEqual((watch.command, watch.demo, watch.source, watch.language), ("watch", True, "desktop", "auto"))
    configure = parser.parse_args(["configure", "--model", "/models/model.bin", "--apply"])
    self.assertEqual((configure.command, configure.model, configure.apply), ("configure", "/models/model.bin", True))
    with mock.patch.object(captions.sys, "stderr", io.StringIO()):
      with self.assertRaises(SystemExit):
        parser.parse_args(["watch", "--source", "network"])

  def test_main_demo_bypasses_real_platform_model_and_commands(self) -> None:
    output = io.StringIO()
    with (
      mock.patch.object(captions.sys, "stdout", output),
      mock.patch.object(captions, "platform_supported", side_effect=AssertionError("real platform queried")),
      mock.patch.object(captions, "discover_model", side_effect=AssertionError("model queried")),
      mock.patch.object(captions, "run_demo", return_value=0) as run_demo,
    ):
      self.assertEqual(captions.main(["watch", "--demo"]), 0)
    run_demo.assert_called_once()


class CleanupTests(unittest.TestCase):
  class _OwnedChild:
    def __init__(self) -> None:
      self.terminate_calls = 0
      self.signals: list[signal.Signals] = []

    def signal(self, requested: signal.Signals) -> bool:
      self.signals.append(requested)
      return True

    def terminate(self) -> None:
      self.terminate_calls += 1

  def test_session_cleanup_is_idempotent_and_resumes_paused_capture_before_termination(self) -> None:
    with tempfile.TemporaryDirectory() as temporary:
      runtime = Path(temporary)
      model = runtime / "model.bin"
      model.write_bytes(b"fixture")
      session = captions.CaptionSession(
        model,
        "microphone",
        "en",
        captions.EventWriter(io.StringIO()),
        runtime,
      )
      capture = self._OwnedChild()
      server = self._OwnedChild()
      session.capture = capture  # type: ignore[assignment]
      session.server = server  # type: ignore[assignment]
      session.paused = True

      session.cleanup()
      session.cleanup()

      self.assertTrue(session.stop_event.is_set())
      self.assertEqual(capture.signals, [signal.SIGTERM, signal.SIGCONT])
      self.assertEqual(capture.terminate_calls, 1)
      self.assertEqual(server.terminate_calls, 1)
      self.assertFalse(session.public_directory.exists())

  def test_broken_startup_stdout_still_closes_session_and_lease(self) -> None:
    class BrokenStream:
      def write(self, _value: str) -> None:
        raise BrokenPipeError("consumer closed")

      def flush(self) -> None:
        return

    class FakeClient:
      def cancel(self) -> None:
        return

    class FakeSession:
      def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.client = FakeClient()
        self.cleanup_called = False

      def cleanup(self) -> None:
        self.cleanup_called = True

    class FakeLease:
      def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.acquired = False
        self.closed = False

      def acquire(self) -> None:
        self.acquired = True

      def close(self) -> None:
        self.closed = True

    with tempfile.TemporaryDirectory() as temporary:
      session = FakeSession()
      lease = FakeLease(Path(temporary))
      writer = captions.EventWriter(BrokenStream())
      with (
        mock.patch.object(captions, "platform_supported", return_value=True),
        mock.patch.object(captions, "runtime_dir", return_value=Path(temporary)),
        mock.patch.object(captions, "SessionLease", return_value=lease),
        mock.patch.object(captions, "CaptionSession", return_value=session),
        mock.patch.object(captions, "arm_parent_death_signal"),
        mock.patch.object(captions.signal, "getsignal", return_value=signal.SIG_DFL),
        mock.patch.object(captions.signal, "signal"),
      ):
        code = captions.run_watch_session(
          model=Path(temporary) / "model.bin",
          source="microphone",
          language="en",
          writer=writer,
          server_binary="/unused/whisper-server",
          capture_binary="/unused/pw-record",
        )
      self.assertEqual(code, 1)
      self.assertTrue(writer.broken.is_set())
      self.assertTrue(lease.acquired)
      self.assertTrue(session.cleanup_called, "session cleanup was skipped after stdout closed")
      self.assertTrue(lease.closed, "session lease was left open after stdout closed")


if __name__ == "__main__":
  unittest.main()
