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
import time
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
        "language": "pt",
      }
      captions.atomic_write_json(path, value)

      self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
      self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
      self.assertEqual(json.loads(path.read_text(encoding="utf-8")), value)
      self.assertEqual(captions.load_config(path), value)

      os.chmod(path, 0o666)
      captions.atomic_write_json(path, value | {"source": "microphone"})
      self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

  def test_language_validation_canonicalizes_supported_bcp47_primary_codes(self) -> None:
    self.assertEqual(captions.validate_language("EN"), "en")
    self.assertEqual(captions.validate_language("pt-BR"), "pt")
    self.assertEqual(captions.validate_language("zh-Hant-TW"), "zh")
    self.assertEqual(captions.validate_language("AUTO"), "auto")
    for value in ("zz", "zz-ZZ", "english", "en_US", "en--US", "en\r\nInjected"):
      with self.subTest(value=value):
        with self.assertRaises(captions.CaptionError) as caught:
          captions.validate_language(value)
        self.assertEqual(caught.exception.code, "invalid-language")

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

  def test_discovery_is_language_aware_and_includes_plugin_model_directory(self) -> None:
    with tempfile.TemporaryDirectory() as temporary:
      root = Path(temporary)
      model_directory = root / "share" / "omarchy-live-captions" / "models"
      model_directory.mkdir(parents=True)
      english = model_directory / "ggml-base.en.bin"
      multilingual = model_directory / "ggml-base.bin"
      for path in (english, multilingual):
        path.write_bytes(b"fixture")
        os.chmod(path, 0o600)
      env = {"HOME": temporary, "XDG_DATA_HOME": str(root / "share")}

      directories = captions.known_model_directories(env)
      self.assertEqual(directories[0], model_directory)
      found, origin, issue = captions.discover_model(environ=env, language="es-MX")
      self.assertEqual((found, origin, issue), (multilingual.resolve(), "discovered", ""))
      found, origin, issue = captions.discover_model(english, environ=env, language="auto")
      self.assertIsNone(found)
      self.assertEqual(origin, "argument")
      self.assertIn("English-only", issue)

  def test_configure_rejects_english_only_model_for_non_english(self) -> None:
    with tempfile.TemporaryDirectory() as temporary:
      model = Path(temporary) / "ggml-small.en-q5_1.bin"
      model.write_bytes(b"fixture")
      os.chmod(model, 0o600)
      with (
        mock.patch.object(captions, "load_config", return_value=captions.default_config()),
        self.assertRaises(captions.CaptionError) as caught,
      ):
        captions.configure_values(model, "microphone", "fr-CA")
      self.assertEqual(caught.exception.code, "incompatible-model")


class DoctorCapabilityTests(unittest.TestCase):
  @staticmethod
  def probe(output: bytes) -> captions.OwnedProcess:
    process = mock.Mock()
    process.stdout = io.BytesIO()
    process.stderr = io.BytesIO(output)
    process.wait.return_value = 0
    process.poll.return_value = 0
    return captions.OwnedProcess(process)

  def test_whisper_help_probe_requires_every_fixed_server_option(self) -> None:
    complete_help = " ".join(captions.REQUIRED_WHISPER_SERVER_FLAGS).encode("utf-8")
    with mock.patch.object(
      captions,
      "spawn_owned",
      return_value=self.probe(complete_help),
    ) as spawn:
      self.assertEqual(
        captions.whisper_server_capability_issue("/usr/bin/whisper-server", "/usr/bin/setpriv"),
        "",
      )
    self.assertEqual(spawn.call_args.kwargs["setpriv_binary"], "/usr/bin/setpriv")

    with mock.patch.object(
      captions,
      "spawn_owned",
      return_value=self.probe(b"--model --host --port"),
    ):
      issue = captions.whisper_server_capability_issue(
        "/usr/bin/whisper-server",
        "/usr/bin/setpriv",
      )
    self.assertIn("--request-path", issue)
    self.assertIn("--public", issue)

  def test_whisper_help_probe_terminates_a_hung_owned_process(self) -> None:
    owned = self.probe(b"")
    owned.process.wait.side_effect = captions.subprocess.TimeoutExpired("whisper-server", 5.0)
    owned.process.poll.return_value = None
    owned.terminate = mock.Mock()
    owned.close_streams = mock.Mock()
    with mock.patch.object(captions, "spawn_owned", return_value=owned):
      issue = captions.whisper_server_capability_issue(
        "/usr/bin/whisper-server",
        "/usr/bin/setpriv",
      )
    self.assertIn("timed out", issue)
    owned.terminate.assert_called_once_with(grace=0.25)
    owned.close_streams.assert_called_once_with()

  def test_diagnostic_drain_caps_retained_output_while_draining_to_eof(self) -> None:
    stream = io.BytesIO(b"x" * (captions.MAX_CAPABILITY_OUTPUT + 8192))
    drain = captions.DiagnosticDrain(
      stream,
      "bounded-output",
      capture_limit=captions.MAX_CAPABILITY_OUTPUT,
    )
    drain.start()
    drain.join(1.0)
    self.assertFalse(drain.thread.is_alive())  # type: ignore[union-attr]
    self.assertEqual(len(drain.captured()), captions.MAX_CAPABILITY_OUTPUT)

  def test_doctor_requires_setpriv_and_an_available_runtime_directory(self) -> None:
    def command(name: str) -> str:
      if name == "setpriv":
        raise captions.CaptionError("missing-command", "Required local command is missing: setpriv")
      return f"/usr/bin/{name}"

    with (
      mock.patch.object(captions, "load_config", return_value=captions.default_config()),
      mock.patch.object(captions, "executable", side_effect=command),
      mock.patch.object(captions, "whisper_server_capability_issue", return_value=""),
      mock.patch.object(captions, "discover_model", return_value=(Path("/models/model.bin"), "argument", "")),
      mock.patch.object(captions, "runtime_dir", side_effect=captions.CaptionError("runtime-unavailable", "No runtime")),
      mock.patch.object(captions, "platform_supported", return_value=True),
    ):
      report = captions.doctor_report(environ={"HOME": "/unused"})
    self.assertFalse(report["ready"])
    self.assertIn("setpriv", report["missing"])
    self.assertIn("private runtime directory", report["missing"])
    self.assertEqual(report["runtimePath"], "")


class OwnedProcessTests(unittest.TestCase):
  def test_spawn_owned_requires_setpriv_with_kill_parent_death_signal(self) -> None:
    process = mock.Mock()
    process.stderr = io.BytesIO()
    with mock.patch.object(captions.subprocess, "Popen", return_value=process) as popen:
      owned = captions.spawn_owned(
        ["/usr/bin/pw-record", "--raw"],
        stdout=captions.subprocess.PIPE,
        label="capture",
        setpriv_binary="/usr/bin/setpriv",
      )
    self.assertIs(owned.process, process)
    command = popen.call_args.args[0]
    self.assertEqual(
      command,
      ["/usr/bin/setpriv", "--pdeathsig", "KILL", "--", "/usr/bin/pw-record", "--raw"],
    )


class AudioAndProtocolTests(unittest.TestCase):
  def test_text_sanitization_preserves_joiners_and_combining_marks(self) -> None:
    self.assertEqual(captions.clean_message("می\u200cروم 👩\u200d💻\u202e"), "می\u200cروم 👩\u200d💻")
    self.assertNotEqual(captions._normalized_word("की"), captions._normalized_word("का"))
    self.assertNotEqual(captions._normalized_word("கி"), captions._normalized_word("கா"))
    self.assertNotEqual(captions._normalized_word("కి"), captions._normalized_word("కా"))

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
    self.assertIn(b'name="language"\r\n\r\npt\r\n', body)

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

  def test_transcript_deduper_removes_exact_cjk_character_overlap(self) -> None:
    deduper = captions.TranscriptDeduper()
    self.assertEqual(deduper.novel_text("今天天气很好我们去公园"), "今天天气很好我们去公园")
    self.assertEqual(deduper.novel_text("我们去公园然后吃饭"), "然后吃饭")
    self.assertEqual(deduper.novel_text("然后吃饭再回家"), "再回家")

  def test_transcript_deduper_does_not_merge_distinct_indic_words(self) -> None:
    deduper = captions.TranscriptDeduper()
    self.assertEqual(deduper.novel_text("की"), "की")
    self.assertEqual(deduper.novel_text("का घर"), "का घर")

  def test_character_fallback_never_trims_ordinary_english_prefixes(self) -> None:
    deduper = captions.TranscriptDeduper()
    self.assertEqual(deduper.novel_text("I need to"), "I need to")
    self.assertEqual(deduper.novel_text("Today we leave"), "Today we leave")
    self.assertEqual(deduper.novel_text("the cat"), "the cat")
    self.assertEqual(deduper.novel_text("catalog search"), "catalog search")

    mixed = captions.TranscriptDeduper()
    self.assertEqual(mixed.novel_text("你好 the cat"), "你好 the cat")
    self.assertEqual(mixed.novel_text("catalog 世界"), "catalog 世界")
    self.assertEqual(mixed.novel_text("今日は rust"), "今日は rust")
    self.assertEqual(mixed.novel_text("rustic 日本語"), "rustic 日本語")

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
    oldest = captions.CapturedChunk(b"oldest", 0, time.monotonic())
    middle = captions.CapturedChunk(b"middle", 0, time.monotonic())
    newest = captions.CapturedChunk(b"newest", 0, time.monotonic())
    reader._put(oldest)
    reader._put(middle)

    producer = threading.Thread(target=reader._put, args=(newest,), daemon=True)
    producer.start()
    producer.join(timeout=0.5)
    self.assertFalse(producer.is_alive(), "capture producer blocked behind inference")
    self.assertTrue(reader.overflowed.is_set())
    self.assertEqual(reader.queue.get_nowait(), middle)
    self.assertEqual(reader.queue.get_nowait(), newest)

  def test_reader_emits_eof_sentinel_and_can_discard_pending(self) -> None:
    reader = captions.CaptureReader(io.BytesIO(b"audio"), threading.Event())
    reader.start()
    reader.thread.join(timeout=1.0)
    self.assertFalse(reader.thread.is_alive())
    chunk = reader.queue.get_nowait()
    self.assertIsInstance(chunk, captions.CapturedChunk)
    assert isinstance(chunk, captions.CapturedChunk)
    self.assertEqual((chunk.data, chunk.epoch), (b"audio", 0))
    self.assertIsNone(reader.queue.get_nowait())
    reader.queue.put_nowait(captions.CapturedChunk(b"stale", 0, time.monotonic()))
    reader.discard_pending()
    self.assertTrue(reader.queue.empty())

  def test_chunk_keeps_epoch_sampled_before_a_blocking_read(self) -> None:
    started = threading.Event()
    release = threading.Event()
    epoch = 3

    class DeferredPipe:
      def __init__(self) -> None:
        self.calls = 0

      def read(self, _size: int) -> bytes:
        self.calls += 1
        if self.calls == 1:
          started.set()
          release.wait(1.0)
          return b"before-transition"
        return b""

    reader = captions.CaptureReader(DeferredPipe(), threading.Event(), lambda: epoch)  # type: ignore[arg-type]
    reader.start()
    self.assertTrue(started.wait(1.0))
    epoch = 4
    release.set()
    reader.thread.join(timeout=1.0)
    chunk = reader.queue.get_nowait()
    assert isinstance(chunk, captions.CapturedChunk)
    self.assertEqual((chunk.data, chunk.epoch), (b"before-transition", 3))

  def test_paused_epoch_drains_pcm_without_queueing_or_overflow(self) -> None:
    payload = b"x" * captions.READ_BYTES * (captions.MAX_QUEUE_CHUNKS + 4)
    reader = captions.CaptureReader(io.BytesIO(payload), threading.Event(), lambda: -1)
    reader.queue = queue.Queue(maxsize=2)
    reader.start()
    reader.thread.join(timeout=1.0)

    self.assertFalse(reader.thread.is_alive())
    self.assertFalse(reader.overflowed.is_set())
    self.assertIsNone(reader.queue.get_nowait())
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
    code = captions.run_demo(captions.EventWriter(output), interval=0.0, source="desktop")
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    self.assertEqual(code, 0)
    self.assertEqual([event["state"] for event in events[:2]], ["starting", "listening"])
    self.assertEqual(events[-1]["state"], "idle")
    self.assertTrue(all(event.get("source") == "desktop" for event in events))
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
      mock.patch.object(captions, "arm_parent_death_signal") as arm_parent_death_signal,
      mock.patch.object(captions, "run_demo", return_value=0) as run_demo,
    ):
      self.assertEqual(captions.main(["watch", "--demo"]), 0)
    run_demo.assert_called_once()
    arm_parent_death_signal.assert_called_once_with()


class CleanupTests(unittest.TestCase):
  class _OwnedChild:
    def __init__(self) -> None:
      self.terminate_calls = 0
      self.close_streams_calls = 0
      self.signals: list[signal.Signals] = []

    def signal(self, requested: signal.Signals) -> bool:
      self.signals.append(requested)
      return True

    def terminate(self) -> None:
      self.terminate_calls += 1

    def close_streams(self) -> None:
      self.close_streams_calls += 1

  def test_session_cleanup_is_idempotent_and_terminates_capture(self) -> None:
    with tempfile.TemporaryDirectory() as temporary:
      runtime = Path(temporary)
      model = runtime / "model.bin"
      model.write_bytes(b"fixture")
      with mock.patch.object(captions, "reserve_loopback_port", return_value=43008):
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
      self.assertEqual(capture.signals, [signal.SIGTERM])
      self.assertEqual(capture.terminate_calls, 1)
      self.assertEqual(server.terminate_calls, 1)
      self.assertEqual(capture.close_streams_calls, 1)
      self.assertEqual(server.close_streams_calls, 1)
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
        mock.patch.object(captions, "executable", return_value="/usr/bin/setpriv"),
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

  def test_cleanup_failure_cannot_skip_lease_close_or_signal_restore(self) -> None:
    class BrokenStream:
      def write(self, _value: str) -> None:
        raise BrokenPipeError("consumer closed")

      def flush(self) -> None:
        return

    class FailingSession:
      def __init__(self) -> None:
        self.stop_event = threading.Event()

      def cleanup(self) -> None:
        raise RuntimeError("cleanup failed")

    class FakeLease:
      def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.closed = False

      def acquire(self) -> None:
        return

      def close(self) -> None:
        self.closed = True

    with tempfile.TemporaryDirectory() as temporary:
      session = FailingSession()
      lease = FakeLease(Path(temporary))
      restored = mock.Mock()
      with (
        mock.patch.object(captions, "platform_supported", return_value=True),
        mock.patch.object(captions, "executable", return_value="/usr/bin/setpriv"),
        mock.patch.object(captions, "runtime_dir", return_value=Path(temporary)),
        mock.patch.object(captions, "SessionLease", return_value=lease),
        mock.patch.object(captions, "CaptionSession", return_value=session),
        mock.patch.object(captions, "arm_parent_death_signal"),
        mock.patch.object(captions.signal, "getsignal", return_value=signal.SIG_DFL),
        mock.patch.object(captions.signal, "signal", restored),
        self.assertRaisesRegex(RuntimeError, "cleanup failed"),
      ):
        captions.run_watch_session(
          model=Path(temporary) / "model.bin",
          source="microphone",
          language="en",
          writer=captions.EventWriter(BrokenStream()),
          server_binary="/unused/whisper-server",
          capture_binary="/unused/pw-record",
        )

      self.assertTrue(lease.closed)
      self.assertGreaterEqual(restored.call_count, 4)


if __name__ == "__main__":
  unittest.main()
