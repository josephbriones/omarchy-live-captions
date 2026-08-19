"""Private, local rolling captions for the Omarchy Live Captions overlay.

Python is the smallest dependable boundary here: Bash cannot keep draining
binary PCM while local HTTP inference blocks, and QML should not parse audio
or build multipart requests. The helper uses only the standard library. Its
stdout is JSONL; diagnostics belong on stderr.
"""

from __future__ import annotations

import argparse
from array import array
from collections import deque
import contextlib
import ctypes
from dataclasses import dataclass
import fcntl
import http.client
import io
import json
import math
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, BinaryIO, Callable, Mapping, Sequence
import unicodedata
import wave


VERSION = "0.1.0"
SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2
WINDOW_SECONDS = 4
OVERLAP_SECONDS = 1
WINDOW_BYTES = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH * WINDOW_SECONDS
OVERLAP_BYTES = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH * OVERLAP_SECONDS
READ_BYTES = 4096
MAX_QUEUE_CHUNKS = 64
MAX_HTTP_RESPONSE = 1_048_576
# Conservative silence-only gate (~-54 dBFS), not an aggressive speech VAD.
SILENCE_RMS_THRESHOLD = 0.002
VALID_SOURCES = ("microphone", "desktop")
REQUIRED_WHISPER_SERVER_FLAGS = ("--model", "--host", "--port", "--request-path", "--public")
LANGUAGE_RE = re.compile(r"^(?:auto|[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*)$")
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
TOKEN_RE = re.compile(r"[^\w']+", re.UNICODE)


class CaptionError(RuntimeError):
  """An expected, user-actionable helper error."""

  def __init__(self, code: str, message: str):
    super().__init__(message)
    self.code = code
    self.message = clean_message(message)


def clean_message(value: object, limit: int = 500) -> str:
  text = CONTROL_CHARS_RE.sub("", str(value or ""))
  text = "".join(
    character
    for character in text
    if unicodedata.category(character) not in ("Cc", "Cf") or character in "\t\n\r"
  )
  text = re.sub(r"\s+", " ", text).strip()
  if len(text) > limit:
    text = text[: max(0, limit - 1)] + "…"
  return text


def compact_json(value: Mapping[str, Any]) -> str:
  return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class EventWriter:
  """Thread-safe JSONL writer used by capture and control threads."""

  def __init__(self, stream: Any = None):
    self.stream = stream if stream is not None else sys.stdout
    self._lock = threading.Lock()
    self.broken = threading.Event()

  def emit(self, event: Mapping[str, Any]) -> bool:
    with self._lock:
      if self.broken.is_set():
        return False
      try:
        self.stream.write(compact_json(event) + "\n")
        self.stream.flush()
        return True
      except (BrokenPipeError, OSError, ValueError):
        self.broken.set()
        return False


def emit_result(value: Mapping[str, Any]) -> None:
  sys.stdout.write(compact_json(value) + "\n")
  sys.stdout.flush()


def config_path(environ: Mapping[str, str] | None = None) -> Path:
  env = os.environ if environ is None else environ
  base = Path(env.get("XDG_CONFIG_HOME") or (Path(env.get("HOME", str(Path.home()))) / ".config"))
  return base / "omarchy" / "live-captions" / "config.json"


def runtime_dir(environ: Mapping[str, str] | None = None) -> Path:
  env = os.environ if environ is None else environ
  base_value = env.get("XDG_RUNTIME_DIR")
  if base_value:
    base = Path(base_value)
  else:
    candidate = Path("/run/user") / str(os.getuid())
    try:
      candidate_info = candidate.lstat()
    except OSError:
      candidate_info = None
    if (
      candidate_info is not None
      and stat.S_ISDIR(candidate_info.st_mode)
      and not stat.S_ISLNK(candidate_info.st_mode)
      and candidate_info.st_uid == os.getuid()
    ):
      base = candidate
    else:
      raise CaptionError(
        "runtime-unavailable",
        "A private XDG runtime directory is required for a real caption session.",
      )
  if not base.is_absolute():
    raise CaptionError("unsafe-runtime", "XDG_RUNTIME_DIR must be an absolute path.")
  try:
    info = base.lstat()
  except OSError as error:
    raise CaptionError("runtime-unavailable", "The XDG runtime directory is unavailable.") from error
  if (
    not stat.S_ISDIR(info.st_mode)
    or stat.S_ISLNK(info.st_mode)
    or info.st_uid != os.getuid()
  ):
    raise CaptionError("unsafe-runtime", "XDG_RUNTIME_DIR must be a user-owned directory, not a symlink.")
  return base / "omarchy-live-captions"


def ensure_private_directory(path: Path) -> Path:
  if path.is_symlink():
    raise CaptionError("unsafe-directory", f"Refusing a symlinked private directory: {path}")
  path.mkdir(mode=0o700, parents=True, exist_ok=True)
  info = path.lstat()
  if info.st_uid != os.getuid():
    raise CaptionError("unsafe-runtime", f"Runtime directory is not owned by the current user: {path}")
  if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
    raise CaptionError("unsafe-runtime", f"Runtime path is not a directory: {path}")
  os.chmod(path, 0o700)
  return path


def default_config() -> dict[str, Any]:
  return {"schemaVersion": 1, "model": "", "source": "microphone", "language": "en"}


def validate_language(value: object) -> str:
  language = str(value or "en").strip()
  if len(language) > 24 or not LANGUAGE_RE.fullmatch(language):
    raise CaptionError("invalid-language", "Language must be 'auto' or a short language token such as en or pt-BR.")
  return language.replace("_", "-")


def validate_source(value: object) -> str:
  source = str(value or "microphone").strip().lower()
  if source not in VALID_SOURCES:
    raise CaptionError("invalid-source", "Audio source must be microphone or desktop.")
  return source


def load_config(path: Path | None = None) -> dict[str, Any]:
  target = config_path() if path is None else path
  values = default_config()
  if not target.exists():
    return values
  try:
    raw = json.loads(target.read_text(encoding="utf-8"))
  except (OSError, UnicodeError, json.JSONDecodeError) as error:
    raise CaptionError("invalid-config", f"Could not read Live Captions configuration: {error}") from error
  if not isinstance(raw, dict):
    raise CaptionError("invalid-config", "Live Captions configuration must be a JSON object.")
  if raw.get("schemaVersion", 1) != 1:
    raise CaptionError("invalid-config", "Unsupported Live Captions configuration schema.")
  if "model" in raw:
    values["model"] = str(raw["model"] or "")
  if "source" in raw:
    values["source"] = validate_source(raw["source"])
  if "language" in raw:
    values["language"] = validate_language(raw["language"])
  return values


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
  ensure_private_directory(path.parent)
  if path.exists() and path.is_dir():
    raise CaptionError("invalid-config", f"Configuration path is a directory: {path}")
  descriptor, temporary_name = tempfile.mkstemp(prefix=".live-captions.", suffix=".json", dir=path.parent)
  temporary = Path(temporary_name)
  try:
    os.fchmod(descriptor, 0o600)
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with os.fdopen(descriptor, "wb", closefd=True) as handle:
      handle.write(payload)
      handle.flush()
      os.fsync(handle.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)
  except BaseException:
    with contextlib.suppress(OSError):
      os.close(descriptor)
    with contextlib.suppress(OSError):
      temporary.unlink()
    raise


def _model_rank(path: Path) -> tuple[int, str]:
  preferences = (
    "ggml-base.en.bin",
    "ggml-small.en.bin",
    "ggml-tiny.en.bin",
    "ggml-base.bin",
    "ggml-small.bin",
    "ggml-tiny.bin",
  )
  try:
    return (preferences.index(path.name), path.name)
  except ValueError:
    return (len(preferences), path.name)


def known_model_directories(environ: Mapping[str, str] | None = None) -> list[Path]:
  env = os.environ if environ is None else environ
  home = Path(env.get("HOME", str(Path.home())))
  data = Path(env.get("XDG_DATA_HOME") or (home / ".local" / "share"))
  cache = Path(env.get("XDG_CACHE_HOME") or (home / ".cache"))
  return [
    data / "voxtype" / "models",
    data / "whisper.cpp" / "models",
    cache / "whisper",
    cache / "whisper.cpp",
    Path("/usr/share/whisper.cpp/models"),
    Path("/usr/share/whisper.cpp"),
  ]


def checked_model_path(value: object, *, require_absolute: bool = True) -> Path:
  text = str(value or "").strip()
  if not text:
    raise CaptionError("model-missing", "No local whisper.cpp GGML model is configured.")
  candidate = Path(text).expanduser()
  if require_absolute and not candidate.is_absolute():
    raise CaptionError("invalid-model", "The model path must be absolute.")
  try:
    resolved = candidate.resolve(strict=True)
    info = resolved.stat()
  except OSError as error:
    raise CaptionError("model-missing", f"The configured model is unavailable: {candidate}") from error
  if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.R_OK):
    raise CaptionError("invalid-model", f"The configured model is not a readable regular file: {resolved}")
  if resolved.suffix.lower() != ".bin":
    raise CaptionError("invalid-model", "The model must be a whisper.cpp GGML .bin file.")
  if info.st_mode & stat.S_IWOTH:
    raise CaptionError("unsafe-model", "Refusing a world-writable model file.")
  return resolved


def discover_model(
  explicit: object = None,
  *,
  config: Mapping[str, Any] | None = None,
  environ: Mapping[str, str] | None = None,
) -> tuple[Path | None, str, str]:
  env = os.environ if environ is None else environ
  configured = default_config() if config is None else config
  candidates: list[tuple[object, str]] = []
  if explicit:
    candidates.append((explicit, "argument"))
  elif env.get("LIVE_CAPTIONS_MODEL"):
    candidates.append((env["LIVE_CAPTIONS_MODEL"], "environment"))
  elif configured.get("model"):
    candidates.append((configured["model"], "configuration"))
  if candidates:
    value, origin = candidates[0]
    try:
      return checked_model_path(value), origin, ""
    except CaptionError as error:
      return None, origin, error.message

  discovered: list[Path] = []
  for directory in known_model_directories(env):
    with contextlib.suppress(OSError):
      discovered.extend(path for path in directory.glob("ggml-*.bin") if path.is_file())
  for candidate in sorted(set(discovered), key=_model_rank):
    try:
      return checked_model_path(candidate), "discovered", ""
    except CaptionError:
      continue
  return None, "none", "No local whisper.cpp GGML model was found. Configure an absolute model path."


def resolved_language(explicit: object, config: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> str:
  env = os.environ if environ is None else environ
  value = explicit or env.get("LIVE_CAPTIONS_LANGUAGE") or config.get("language") or "en"
  return validate_language(value)


def executable(name: str) -> str:
  path = shutil.which(name)
  if not path:
    raise CaptionError("missing-command", f"Required local command is missing: {name}")
  resolved = Path(path).resolve()
  if not resolved.is_file() or not os.access(resolved, os.X_OK):
    raise CaptionError("missing-command", f"Required local command is not executable: {name}")
  return str(resolved)


def whisper_server_capability_issue(binary: str) -> str:
  """Return a compatibility problem without trusting the command's status."""
  try:
    completed = subprocess.run(
      [binary, "--help"],
      stdin=subprocess.DEVNULL,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      timeout=5.0,
      check=False,
    )
  except subprocess.TimeoutExpired:
    return "whisper-server --help timed out; version 1.7.6 or newer is required."
  except OSError as error:
    return f"Could not inspect whisper-server capabilities: {clean_message(error)}"
  # Current releases print help to stderr and may exit zero for unknown
  # options, so inspect bounded option text rather than the exit status.
  output = (completed.stdout + b"\n" + completed.stderr)[:262_144].decode("utf-8", errors="replace")
  missing = [flag for flag in REQUIRED_WHISPER_SERVER_FLAGS if flag not in output]
  if missing:
    return (
      "whisper-server is missing required local API options "
      + ", ".join(missing)
      + "; version 1.7.6 or newer is required."
    )
  return ""


def platform_supported() -> bool:
  return sys.platform.startswith("linux")


def doctor_report(
  *,
  model: object = None,
  source: object = None,
  language: object = None,
  environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
  env = os.environ if environ is None else environ
  missing: list[str] = []
  issues: list[str] = []
  try:
    config = load_config(config_path(env))
  except CaptionError as error:
    config = default_config()
    missing.append("configuration")
    issues.append(error.message)
  try:
    selected_source = validate_source(source or config.get("source"))
  except CaptionError as error:
    selected_source = "microphone"
    missing.append("audio source")
    issues.append(error.message)
  try:
    selected_language = resolved_language(language, config, env)
  except CaptionError as error:
    selected_language = "en"
    missing.append("language")
    issues.append(error.message)

  command_paths: dict[str, str] = {}
  for command_name in ("pw-record", "whisper-server"):
    try:
      command_paths[command_name] = executable(command_name)
    except CaptionError as error:
      missing.append(command_name)
      issues.append(error.message)
  whisper_binary = command_paths.get("whisper-server")
  if whisper_binary:
    capability_issue = whisper_server_capability_issue(whisper_binary)
    if capability_issue:
      missing.append("compatible whisper-server")
      issues.append(capability_issue)
  model_path, model_origin, model_issue = discover_model(model, config=config, environ=env)
  if model_path is None:
    missing.append("model")
    issues.append(model_issue)
  if not platform_supported():
    missing.append("Linux PipeWire session")
    issues.append("Real audio capture requires Linux with PipeWire; demo mode is still available.")

  missing = list(dict.fromkeys(missing))
  issues = list(dict.fromkeys(filter(None, issues)))
  ready = not missing
  message = (
    f"Dependencies found for local {selected_source} captions; the audio route is checked at Start."
    if ready
    else "Local caption setup needs attention: " + ", ".join(missing) + "."
  )
  return {
    "ok": ready,
    "ready": ready,
    "version": VERSION,
    "backendVersion": "whisper.cpp local server",
    "message": message,
    "missing": missing,
    "issues": issues,
    "modelPath": str(model_path) if model_path else "",
    "modelOrigin": model_origin,
    "source": selected_source,
    "language": selected_language,
    "commands": command_paths,
    "windowMs": WINDOW_SECONDS * 1000,
    "overlapMs": OVERLAP_SECONDS * 1000,
    "retainsAudio": False,
    "retainsTranscript": False,
  }


def configure_values(model: object, source: object, language: object) -> dict[str, Any]:
  current = load_config()
  selected_model = checked_model_path(model)
  selected_source = validate_source(source or current.get("source"))
  selected_language = validate_language(language or current.get("language"))
  return {
    "schemaVersion": 1,
    "model": str(selected_model),
    "source": selected_source,
    "language": selected_language,
  }


def pcm_to_wav(pcm: bytes) -> bytes:
  """Wrap mono signed-16 PCM in an in-memory 16 kHz WAV container."""
  if len(pcm) % SAMPLE_WIDTH:
    pcm = pcm[: len(pcm) - (len(pcm) % SAMPLE_WIDTH)]
  output = io.BytesIO()
  with wave.open(output, "wb") as wav:
    wav.setnchannels(CHANNELS)
    wav.setsampwidth(SAMPLE_WIDTH)
    wav.setframerate(SAMPLE_RATE)
    wav.writeframes(pcm)
  return output.getvalue()


def pcm_level(pcm: bytes) -> float:
  usable = len(pcm) - (len(pcm) % SAMPLE_WIDTH)
  if usable <= 0:
    return 0.0
  samples = array("h")
  samples.frombytes(pcm[:usable])
  if sys.byteorder != "little":
    samples.byteswap()
  if not samples:
    return 0.0
  mean_square = sum(sample * sample for sample in samples) / len(samples)
  return max(0.0, min(1.0, math.sqrt(mean_square) / 32768.0))


def _normalized_word(word: str) -> str:
  return TOKEN_RE.sub("", word).casefold()


class TranscriptDeduper:
  """Remove the exact word overlap introduced by rolling audio windows."""

  def __init__(self, history_limit: int = 96, compare_limit: int = 48):
    self.history_limit = max(8, history_limit)
    self.compare_limit = max(4, min(compare_limit, self.history_limit))
    self._history: list[str] = []

  @property
  def history(self) -> tuple[str, ...]:
    return tuple(self._history)

  def novel_text(self, text: object) -> str:
    original = clean_message(text, 4000).split()
    pairs = [(word, _normalized_word(word)) for word in original]
    pairs = [(word, normalized) for word, normalized in pairs if normalized]
    if not pairs:
      return ""
    normalized = [item[1] for item in pairs]
    maximum = min(len(normalized), len(self._history), self.compare_limit)
    overlap = 0
    for size in range(maximum, 0, -1):
      if self._history[-size:] == normalized[:size]:
        overlap = size
        break
    novel_pairs = pairs[overlap:]
    if not novel_pairs:
      return ""
    novel = " ".join(item[0] for item in novel_pairs)
    self._history.extend(item[1] for item in novel_pairs)
    if len(self._history) > self.history_limit:
      self._history = self._history[-self.history_limit :]
    return novel


def multipart_inference_body(wav_bytes: bytes, language: str) -> tuple[bytes, str]:
  boundary = "live-captions-" + secrets.token_hex(16)
  chunks: list[bytes] = []

  def field(name: str, value: str) -> None:
    chunks.extend(
      [
        f"--{boundary}\r\n".encode("ascii"),
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
        value.encode("utf-8"),
        b"\r\n",
      ]
    )

  chunks.extend(
    [
      f"--{boundary}\r\n".encode("ascii"),
      b'Content-Disposition: form-data; name="file"; filename="chunk.wav"\r\n',
      b"Content-Type: audio/wav\r\n\r\n",
      wav_bytes,
      b"\r\n",
    ]
  )
  field("response_format", "json")
  field("language", language)
  chunks.append(f"--{boundary}--\r\n".encode("ascii"))
  return b"".join(chunks), boundary


class LocalWhisperClient:
  """Direct loopback client; intentionally bypasses proxy-aware libraries."""

  def __init__(self, port: int, request_token: str, timeout: float = 90.0):
    if not re.fullmatch(r"[a-f0-9]{32}", request_token):
      raise CaptionError("invalid-token", "Invalid local inference request token.")
    self.port = int(port)
    self.prefix = "/" + request_token
    self.timeout = max(0.05, min(float(timeout), 180.0))
    self._active: http.client.HTTPConnection | None = None
    self._cancelled = threading.Event()
    # Signal handlers may request cancellation while the main thread is
    # changing this pointer; re-entrancy avoids self-deadlock.
    self._active_lock = threading.RLock()

  def _set_active(self, connection: http.client.HTTPConnection | None) -> None:
    with self._active_lock:
      self._active = connection

  def close_active(self) -> None:
    with self._active_lock:
      connection = self._active
    if connection is not None:
      with contextlib.suppress(OSError, http.client.HTTPException):
        connection.close()

  def cancel(self) -> None:
    self._cancelled.set()
    self.close_active()

  def healthy(self) -> bool:
    if self._cancelled.is_set():
      return False
    connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=0.5)
    self._set_active(connection)
    try:
      if self._cancelled.is_set():
        return False
      connection.request("GET", self.prefix + "/health", headers={"Connection": "close"})
      response = connection.getresponse()
      payload = response.read(MAX_HTTP_RESPONSE + 1)
      if response.status != 200 or len(payload) > MAX_HTTP_RESPONSE:
        return False
      parsed = json.loads(payload.decode("utf-8"))
      return isinstance(parsed, dict) and parsed.get("status") == "ok"
    except (OSError, TimeoutError, UnicodeError, json.JSONDecodeError, http.client.HTTPException):
      return False
    finally:
      self._set_active(None)
      connection.close()

  def transcribe(self, wav_bytes: bytes, language: str) -> str:
    if self._cancelled.is_set():
      raise CaptionError("stopped", "Caption inference was stopped.")
    body, boundary = multipart_inference_body(wav_bytes, language)
    connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
    self._set_active(connection)
    try:
      if self._cancelled.is_set():
        raise CaptionError("stopped", "Caption inference was stopped.")
      connection.request(
        "POST",
        self.prefix + "/inference",
        body=body,
        headers={
          "Content-Type": f"multipart/form-data; boundary={boundary}",
          "Content-Length": str(len(body)),
          "Connection": "close",
        },
      )
      response = connection.getresponse()
      payload = response.read(MAX_HTTP_RESPONSE + 1)
      if len(payload) > MAX_HTTP_RESPONSE:
        raise CaptionError("inference-response", "Local inference response exceeded the safety limit.")
      if response.status != 200:
        raise CaptionError("inference-http", f"Local inference returned HTTP {response.status}.")
      try:
        parsed = json.loads(payload.decode("utf-8"))
      except (UnicodeError, json.JSONDecodeError) as error:
        raise CaptionError("inference-json", "Local inference returned malformed JSON.") from error
      if not isinstance(parsed, dict) or not isinstance(parsed.get("text"), str):
        raise CaptionError("inference-schema", "Local inference response did not contain caption text.")
      return clean_message(parsed["text"], 4000)
    except CaptionError:
      raise
    except (OSError, TimeoutError, http.client.HTTPException) as error:
      raise CaptionError("inference-unavailable", f"Local inference request failed: {error}") from error
    finally:
      self._set_active(None)
      connection.close()


def reserve_loopback_port() -> int:
  with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
    listener.bind(("127.0.0.1", 0))
    return int(listener.getsockname()[1])


def _parent_death_signal() -> None:
  if not sys.platform.startswith("linux"):
    return
  try:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(1, int(signal.SIGTERM), 0, 0, 0)
  except (AttributeError, OSError):
    return


def arm_parent_death_signal() -> None:
  if not sys.platform.startswith("linux"):
    return
  parent = os.getppid()
  _parent_death_signal()
  if os.getppid() != parent:
    os.kill(os.getpid(), signal.SIGTERM)


class DiagnosticDrain:
  """Continuously drain a child pipe while retaining only a few safe lines."""

  def __init__(self, pipe: BinaryIO | None, label: str):
    self.pipe = pipe
    self.label = label
    self.lines: deque[str] = deque(maxlen=8)
    self.thread: threading.Thread | None = None

  def start(self) -> None:
    if self.pipe is None:
      return
    self.thread = threading.Thread(target=self._run, name=f"{self.label}-drain", daemon=True)
    self.thread.start()

  def _run(self) -> None:
    assert self.pipe is not None
    while True:
      chunk = self.pipe.readline(4096)
      if not chunk:
        return
      self.lines.append(clean_message(chunk.decode("utf-8", errors="replace"), 300))

  def latest(self) -> str:
    return next((line for line in reversed(self.lines) if line), "")


@dataclass
class OwnedProcess:
  process: subprocess.Popen[bytes]
  stdout_drain: DiagnosticDrain | None = None
  stderr_drain: DiagnosticDrain | None = None

  def signal(self, requested: signal.Signals) -> bool:
    # An unreaped live child cannot have its PID reused. start_new_session
    # makes that PID the process-group id, so signals stay inside the
    # exact subprocess tree represented by this Popen handle.
    if self.process.poll() is not None:
      return False
    try:
      if os.getpgid(self.process.pid) != self.process.pid:
        return False
      os.killpg(self.process.pid, requested)
      return True
    except OSError:
      return False

  def terminate(self, grace: float = 2.0) -> None:
    if self.process.poll() is not None:
      return
    self.signal(signal.SIGTERM)
    try:
      self.process.wait(timeout=grace)
      return
    except subprocess.TimeoutExpired:
      pass
    self.signal(signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
      self.process.wait(timeout=1.0)


def spawn_owned(command: Sequence[str], *, stdout: int | BinaryIO, label: str) -> OwnedProcess:
  actual_command = list(command)
  setpriv = shutil.which("setpriv") if sys.platform.startswith("linux") else None
  if setpriv:
    actual_command = [str(Path(setpriv).resolve()), "--pdeathsig", "TERM", "--", *actual_command]
  process = subprocess.Popen(
    actual_command,
    stdin=subprocess.DEVNULL,
    stdout=stdout,
    stderr=subprocess.PIPE,
    start_new_session=True,
    close_fds=True,
  )
  owned = OwnedProcess(process=process)
  owned.stderr_drain = DiagnosticDrain(process.stderr, label + "-stderr")
  return owned


def whisper_server_command(
  binary: str,
  model: Path,
  port: int,
  request_token: str,
  public_directory: Path,
) -> list[str]:
  return [
    binary,
    "--model",
    str(model),
    "--host",
    "127.0.0.1",
    "--port",
    str(port),
    "--request-path",
    "/" + request_token,
    "--public",
    str(public_directory),
  ]


def capture_command(binary: str, source: str) -> list[str]:
  command = [
    binary,
    "--raw",
    "--rate=16000",
    "--channels=1",
    "--channel-map=mono",
    "--format=s16",
    "--latency=50ms",
  ]
  if source == "desktop":
    command.append('--properties={"stream.capture.sink":true}')
  command.append("-")
  return command


class SessionLease:
  """Advisory single-session lock in the private runtime directory."""

  def __init__(self, directory: Path):
    self.directory = directory
    self.lock_path = directory / "session.lock"
    self._descriptor: int | None = None

  def acquire(self) -> None:
    ensure_private_directory(self.directory)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
      descriptor = os.open(self.lock_path, flags, 0o600)
    except OSError as error:
      raise CaptionError("unsafe-lock", f"Could not open the private caption lock: {error}") from error
    try:
      info = os.fstat(descriptor)
      if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise CaptionError("unsafe-lock", "Caption session lock is not a user-owned regular file.")
      os.fchmod(descriptor, 0o600)
      try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
      except BlockingIOError as error:
        raise CaptionError("already-running", "A Live Captions session is already active.") from error
      self._descriptor = descriptor
    except BaseException:
      os.close(descriptor)
      raise

  def close(self) -> None:
    if self._descriptor is None:
      return
    with contextlib.suppress(OSError):
      fcntl.flock(self._descriptor, fcntl.LOCK_UN)
    os.close(self._descriptor)
    self._descriptor = None


class ControlReader:
  """Read pause/resume commands from the owning QML Process."""

  def __init__(self, stream: Any, callback: Callable[[str], Mapping[str, Any]], writer: EventWriter):
    self.stream = stream
    self.callback = callback
    self.writer = writer
    self.thread = threading.Thread(target=self._run, name="caption-control", daemon=True)

  def start(self) -> None:
    self.thread.start()

  def _run(self) -> None:
    while True:
      try:
        raw = self.stream.readline(65)
      except (OSError, ValueError):
        return
      if not raw:
        return
      command = clean_message(raw, 64)
      if command not in ("pause", "resume"):
        continue
      try:
        self.callback(command)
      except CaptionError as error:
        self.writer.emit({"type": "error", "code": error.code, "message": error.message})


class CaptureReader:
  def __init__(self, pipe: BinaryIO, stop_event: threading.Event):
    self.pipe = pipe
    self.stop_event = stop_event
    self.queue: queue.Queue[bytes | None] = queue.Queue(maxsize=MAX_QUEUE_CHUNKS)
    self.overflowed = threading.Event()
    self.thread = threading.Thread(target=self._run, name="caption-audio-reader", daemon=True)

  def start(self) -> None:
    self.thread.start()

  def _put(self, value: bytes | None) -> None:
    while not self.stop_event.is_set():
      try:
        self.queue.put_nowait(value)
        return
      except queue.Full:
        # Audio capture must never block behind inference. Drop the
        # oldest chunk, mark a discontinuity, and keep draining.
        with contextlib.suppress(queue.Empty):
          self.queue.get_nowait()
        self.overflowed.set()

  def _run(self) -> None:
    try:
      while not self.stop_event.is_set():
        chunk = self.pipe.read(READ_BYTES)
        if not chunk:
          break
        self._put(chunk)
    finally:
      self._put(None)

  def discard_pending(self) -> None:
    while True:
      try:
        self.queue.get_nowait()
      except queue.Empty:
        return


class CaptionSession:
  def __init__(self, model: Path, source: str, language: str, writer: EventWriter, private_runtime: Path):
    self.model = model
    self.source = source
    self.language = language
    self.writer = writer
    self.stop_event = threading.Event()
    self.paused = False
    self.started_at = time.monotonic()
    self.port = reserve_loopback_port()
    self.request_token = secrets.token_hex(16)
    self.public_directory = private_runtime / ("empty-public-" + self.request_token)
    ensure_private_directory(self.public_directory)
    if any(self.public_directory.iterdir()):
      raise CaptionError("unsafe-public", "Local inference public directory was not empty.")
    self.client = LocalWhisperClient(self.port, self.request_token)
    self.server: OwnedProcess | None = None
    self.capture: OwnedProcess | None = None
    self.reader: CaptureReader | None = None
    self._cleanup_lock = threading.Lock()
    self._state_lock = threading.Lock()
    self._cleaned = False
    self.transition_epoch = 0

  def start_server(self, server_binary: str) -> None:
    if self.stop_event.is_set():
      raise CaptionError("stopped", "Caption startup was stopped.")
    self.server = spawn_owned(
      whisper_server_command(
        server_binary,
        self.model,
        self.port,
        self.request_token,
        self.public_directory,
      ),
      stdout=subprocess.PIPE,
      label="whisper-server",
    )
    if self.stop_event.is_set():
      self.request_stop()
      raise CaptionError("stopped", "Caption startup was stopped.")
    assert self.server.process.stdout is not None
    self.server.stdout_drain = DiagnosticDrain(self.server.process.stdout, "whisper-server-stdout")
    for drain in (self.server.stdout_drain, self.server.stderr_drain):
      if drain is not None:
        drain.start()

  def start_capture(self, capture_binary: str) -> None:
    if self.stop_event.is_set():
      raise CaptionError("stopped", "Caption startup was stopped.")
    self.capture = spawn_owned(
      capture_command(capture_binary, self.source),
      stdout=subprocess.PIPE,
      label="pw-record",
    )
    if self.stop_event.is_set():
      self.request_stop()
      raise CaptionError("stopped", "Caption startup was stopped.")
    assert self.capture.process.stdout is not None
    if self.capture.stderr_drain is not None:
      self.capture.stderr_drain.start()
    self.reader = CaptureReader(self.capture.process.stdout, self.stop_event)
    self.reader.start()

  def _status(self, state: str) -> dict[str, Any]:
    return {
      "type": "status",
      "state": state,
      "source": self.source,
      "elapsedSeconds": max(0, int(time.monotonic() - self.started_at)),
    }

  def handle_control(self, command: str) -> Mapping[str, Any]:
    if command == "pause":
      with self._state_lock:
        if self.paused:
          return {"ok": True, "state": "paused"}
      if self.capture is None or not self.capture.signal(signal.SIGSTOP):
        raise CaptionError("capture-stopped", "The owned audio capture is no longer running.")
      with self._state_lock:
        self.paused = True
        self.transition_epoch += 1
      if self.reader is not None:
        self.reader.discard_pending()
      self.writer.emit(self._status("paused"))
      return {"ok": True, "state": "paused"}
    if command == "resume":
      with self._state_lock:
        if not self.paused:
          raise CaptionError("not-paused", "The caption session is not paused.")
      if self.reader is not None:
        self.reader.discard_pending()
      if self.capture is None or not self.capture.signal(signal.SIGCONT):
        raise CaptionError("capture-stopped", "The owned audio capture is no longer running.")
      with self._state_lock:
        self.paused = False
        self.transition_epoch += 1
      self.writer.emit(self._status("listening"))
      return {"ok": True, "state": "listening"}
    raise CaptionError("unknown-control", "Unknown caption control command.")

  def wait_until_ready(self, timeout: float = 90.0) -> None:
    assert self.server is not None
    deadline = time.monotonic() + max(2.0, min(timeout, 180.0))
    while not self.stop_event.is_set() and time.monotonic() < deadline:
      if self.server.process.poll() is not None:
        detail = self.server.stderr_drain.latest() if self.server.stderr_drain else ""
        raise CaptionError("server-exited", detail or "Local whisper-server exited while loading the model.")
      if self.client.healthy():
        return
      self.stop_event.wait(0.2)
    if self.stop_event.is_set():
      raise CaptionError("stopped", "Caption startup was stopped.")
    raise CaptionError("server-timeout", "Local whisper-server did not become healthy before the startup timeout.")

  def cleanup(self) -> None:
    with self._cleanup_lock:
      if self._cleaned:
        return
      self._cleaned = True
      self.request_stop()
      if self.capture is not None:
        self.capture.terminate()
      if self.server is not None:
        self.server.terminate()
      with contextlib.suppress(OSError):
        self.public_directory.rmdir()

  def request_stop(self) -> None:
    """Latch cancellation and stop audio capture without waiting for inference."""
    self.stop_event.set()
    if self.capture is not None:
      # SIGTERM remains pending for a stopped process until it continues.
      # Send both without waiting so closing the overlay ends capture now.
      self.capture.signal(signal.SIGTERM)
      self.capture.signal(signal.SIGCONT)
      self.paused = False
    self.client.cancel()

  def run_caption_loop(self) -> int:
    if self.reader is None or self.capture is None:
      raise CaptionError("capture-start", "Owned capture reader was not started.")
    audio = bytearray()
    deduper = TranscriptDeduper()
    sequence = 0
    next_heartbeat = time.monotonic() + 1.0
    next_level = 0.0
    reached_eof = False
    local_epoch = self.transition_epoch

    while not self.stop_event.is_set():
      if self.writer.broken.is_set():
        self.stop_event.set()
        break
      with self._state_lock:
        paused = self.paused
        epoch = self.transition_epoch
      if epoch != local_epoch:
        local_epoch = epoch
        audio.clear()
        deduper = TranscriptDeduper()
      if paused:
        audio.clear()
        if self.capture.process.poll() is not None:
          reached_eof = True
          break
        try:
          self.reader.queue.get(timeout=0.2)
        except queue.Empty:
          pass
        continue
      now = time.monotonic()
      if now >= next_heartbeat:
        self.writer.emit(
          {
            "type": "heartbeat",
            "state": "paused" if self.paused else "listening",
            "source": self.source,
            "elapsedSeconds": max(0, int(now - self.started_at)),
          }
        )
        next_heartbeat = now + 1.0
      try:
        chunk = self.reader.queue.get(timeout=0.2)
      except queue.Empty:
        if self.capture.process.poll() is not None:
          reached_eof = True
          break
        continue
      if chunk is None:
        reached_eof = True
        break
      if self.reader.overflowed.is_set():
        self.reader.overflowed.clear()
        audio.clear()
        deduper = TranscriptDeduper()
      audio.extend(chunk)
      if now >= next_level:
        self.writer.emit(
          {
            "type": "level",
            "source": self.source,
            "value": round(pcm_level(chunk), 4),
          }
        )
        next_level = now + 0.25

      while len(audio) >= WINDOW_BYTES and not self.stop_event.is_set():
        window_pcm = bytes(audio[:WINDOW_BYTES])
        del audio[: WINDOW_BYTES - OVERLAP_BYTES]
        inference_epoch = local_epoch
        if pcm_level(window_pcm) < SILENCE_RMS_THRESHOLD:
          sequence += 1
          continue
        inference_started = time.monotonic()
        try:
          raw_text = self.client.transcribe(pcm_to_wav(window_pcm), self.language)
        except CaptionError:
          if self.stop_event.is_set():
            break
          raise
        inference_ms = max(0, int((time.monotonic() - inference_started) * 1000))
        with self._state_lock:
          if self.paused or self.transition_epoch != inference_epoch:
            audio.clear()
            deduper = TranscriptDeduper()
            local_epoch = self.transition_epoch
            sequence += 1
            continue
        text = deduper.novel_text(raw_text)
        end_ms = sequence * (WINDOW_SECONDS - OVERLAP_SECONDS) * 1000 + WINDOW_SECONDS * 1000
        if text:
          self.writer.emit(
            {
              "type": "caption",
              "kind": "final",
              "seq": sequence,
              "startMs": max(0, end_ms - WINDOW_SECONDS * 1000),
              "endMs": end_ms,
              "text": text,
              "source": self.source,
              "latencyMs": WINDOW_SECONDS * 1000 + inference_ms,
            }
          )
        sequence += 1

    if self.stop_event.is_set():
      return 0
    if reached_eof:
      return_code = self.capture.process.poll()
      if return_code not in (None, 0):
        detail = self.capture.stderr_drain.latest() if self.capture.stderr_drain else ""
        raise CaptionError("capture-exited", detail or f"Audio capture exited with code {return_code}.")
      self.writer.emit(self._status("idle"))
      return 0
    return 0


DEMO_CAPTIONS = (
  ("microphone", "Live captions stay visible without blocking the app beneath."),
  ("desktop", "Choose microphone or desktop audio for each private local session."),
  ("desktop", "Rolling overlap keeps words at window boundaries from disappearing."),
  ("microphone", "No audio recording or transcript file is retained."),
)


def run_demo(writer: EventWriter, interval: float = 0.65) -> int:
  stopped = threading.Event()

  def handle_signal(_number: int, _frame: Any) -> None:
    stopped.set()

  previous: dict[int, Any] = {}
  if threading.current_thread() is threading.main_thread():
    for number in (signal.SIGINT, signal.SIGTERM):
      previous[number] = signal.getsignal(number)
      signal.signal(number, handle_signal)
  started = time.monotonic()
  try:
    writer.emit({"type": "status", "state": "starting", "source": "microphone", "elapsedSeconds": 0})
    if writer.broken.is_set():
      return 1
    if stopped.wait(max(0.0, interval / 2)):
      return 0
    writer.emit({"type": "status", "state": "listening", "source": "microphone", "elapsedSeconds": 0})
    for sequence, (source, text) in enumerate(DEMO_CAPTIONS):
      if writer.broken.is_set():
        break
      if stopped.wait(max(0.0, interval)):
        break
      writer.emit({"type": "level", "source": source, "value": 0.32 + (sequence % 3) * 0.18})
      end_ms = (sequence + 1) * 3000 + 1000
      writer.emit(
        {
          "type": "caption",
          "kind": "final",
          "seq": sequence,
          "startMs": max(0, end_ms - 4000),
          "endMs": end_ms,
          "text": text,
          "source": source,
          "latencyMs": 4200 + sequence * 140,
        }
      )
      writer.emit(
        {
          "type": "heartbeat",
          "state": "recording",
          "source": source,
          "elapsedSeconds": max(0, int(time.monotonic() - started)),
        }
      )
    return 0
  finally:
    if threading.current_thread() is threading.main_thread():
      for number, handler in previous.items():
        signal.signal(number, handler)


def run_watch_session(
  *,
  model: Path,
  source: str,
  language: str,
  writer: EventWriter,
  server_binary: str | None = None,
  capture_binary: str | None = None,
  startup_timeout: float = 90.0,
) -> int:
  if not platform_supported():
    raise CaptionError("unsupported-platform", "Real audio capture requires Linux with PipeWire.")
  server_command = executable("whisper-server") if server_binary is None else server_binary
  recorder_command = executable("pw-record") if capture_binary is None else capture_binary
  lease = SessionLease(runtime_dir())
  lease.acquire()
  try:
    session = CaptionSession(model, source, language, writer, lease.directory)
  except BaseException:
    lease.close()
    raise
  previous: dict[int, Any] = {}

  def handle_signal(_number: int, _frame: Any) -> None:
    session.request_stop()

  if threading.current_thread() is threading.main_thread():
    for number in (signal.SIGINT, signal.SIGTERM):
      previous[number] = signal.getsignal(number)
      signal.signal(number, handle_signal)
  try:
    arm_parent_death_signal()
    writer.emit({"type": "status", "state": "starting", "source": source, "elapsedSeconds": 0})
    if writer.broken.is_set():
      return 1
    ControlReader(sys.stdin, session.handle_control, writer).start()
    session.start_server(server_command)
    session.wait_until_ready(startup_timeout)
    if session.stop_event.is_set():
      return 0
    # Audio capture starts only after the local model reports healthy, so
    # startup never queues stale pre-ready speech.
    session.start_capture(recorder_command)
    writer.emit(session._status("listening"))
    return session.run_caption_loop()
  except CaptionError as error:
    if error.code == "stopped" or session.stop_event.is_set():
      return 0
    writer.emit({"type": "error", "code": error.code, "message": error.message})
    sys.stderr.write(error.message + "\n")
    sys.stderr.flush()
    return 1
  except OSError as error:
    message = clean_message(error)
    writer.emit({"type": "error", "code": "process-start", "message": message})
    sys.stderr.write(message + "\n")
    sys.stderr.flush()
    return 1
  finally:
    session.cleanup()
    lease.close()
    if threading.current_thread() is threading.main_thread():
      for number, handler in previous.items():
        signal.signal(number, handler)


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(
    prog="live-captions",
    description="Private local rolling captions for Omarchy.",
  )
  parser.add_argument("--version", action="version", version=VERSION)
  commands = parser.add_subparsers(dest="command", required=True)

  doctor = commands.add_parser("doctor", help="Check local capture, inference, and model readiness.")
  doctor.add_argument("--model")
  doctor.add_argument("--source", choices=VALID_SOURCES)
  doctor.add_argument("--language")

  configure = commands.add_parser("configure", help="Preview or explicitly write plugin-only preferences.")
  configure.add_argument("--model", required=True)
  configure.add_argument("--source", choices=VALID_SOURCES)
  configure.add_argument("--language")
  configure.add_argument("--apply", action="store_true")

  watch = commands.add_parser("watch", help="Run a foreground caption event stream.")
  watch.add_argument("--demo", action="store_true")
  watch.add_argument("--model")
  watch.add_argument("--source", choices=VALID_SOURCES)
  watch.add_argument("--language")

  return parser


def main(argv: Sequence[str] | None = None) -> int:
  parser = build_parser()
  args = parser.parse_args(argv)
  try:
    if args.command == "doctor":
      report = doctor_report(model=args.model, source=args.source, language=args.language)
      emit_result(report)
      return 0 if report["ready"] else 1
    if args.command == "configure":
      values = configure_values(args.model, args.source, args.language)
      target = config_path()
      if args.apply:
        atomic_write_json(target, values)
      emit_result(
        {
          "ok": True,
          "applied": bool(args.apply),
          "path": str(target),
          "config": values,
          "message": "Live Captions preferences updated." if args.apply else "Preview only; rerun with --apply to write.",
        }
      )
      return 0
    if args.command == "watch":
      writer = EventWriter()
      if args.demo:
        return run_demo(writer)
      config = load_config()
      source = validate_source(args.source or config.get("source"))
      language = resolved_language(args.language, config)
      model, _origin, issue = discover_model(args.model, config=config)
      if model is None:
        writer.emit({"type": "error", "code": "model-missing", "message": issue})
        return 1
      return run_watch_session(model=model, source=source, language=language, writer=writer)
  except CaptionError as error:
    emit_result({"ok": False, "code": error.code, "message": error.message})
    sys.stderr.write(error.message + "\n")
    return 1
  except KeyboardInterrupt:
    return 130
  parser.error("unknown command")
  return 2


if __name__ == "__main__":
  raise SystemExit(main())
