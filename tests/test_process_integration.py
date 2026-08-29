from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import live_captions as captions  # noqa: E402


FAKE_SETPRIV = r"""
#!/usr/bin/env python3
import os
import sys

separator = sys.argv.index("--")
command = sys.argv[separator + 1:]
if not command:
  raise SystemExit("missing command")
os.execv(command[0], command)
"""


FAKE_WHISPER_SERVER = r"""
#!/usr/bin/env python3
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import signal
import sys


def option(name):
  index = sys.argv.index(name)
  return sys.argv[index + 1]


port = int(option("--port"))
prefix = option("--request-path").rstrip("/")
Path(os.environ["FAKE_SERVER_PID_FILE"]).write_text(str(os.getpid()), encoding="utf-8")
health_file = Path(os.environ["FAKE_HEALTH_FILE"])
inference_file = Path(os.environ["FAKE_INFERENCE_FILE"])
stopping = False


class Handler(BaseHTTPRequestHandler):
  def log_message(self, _format, *_args):
    return

  def reply(self, status, payload):
    body = json.dumps(payload).encode("utf-8")
    self.send_response(status)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Connection", "close")
    self.end_headers()
    self.wfile.write(body)

  def do_GET(self):
    if self.path != prefix + "/health":
      self.reply(404, {"status": "missing"})
      return
    health_file.write_text("healthy", encoding="utf-8")
    self.reply(200, {"status": "ok"})

  def do_POST(self):
    if self.path != prefix + "/inference":
      self.reply(404, {"status": "missing"})
      return
    body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
    inference_file.write_text("wav" if b"RIFF" in body else "invalid", encoding="utf-8")
    self.reply(200, {"text": "A deterministic integration caption."})


def stop(_number, _frame):
  global stopping
  stopping = True


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
server = HTTPServer(("127.0.0.1", port), Handler)
server.timeout = 0.1
try:
  while not stopping:
    server.handle_request()
finally:
  server.server_close()
"""


FAKE_PW_RECORD = r"""
#!/usr/bin/env python3
import os
from pathlib import Path
import struct
import time

Path(os.environ["FAKE_CAPTURE_PID_FILE"]).write_text(str(os.getpid()), encoding="utf-8")
# Write complete reader-sized blocks; leaving a partial BufferedReader read
# pending would intentionally delay the last bytes until this process exits.
pcm = struct.pack("<h", 1200) * 65536
view = memoryview(pcm)
while view:
  written = os.write(1, view[:4096])
  view = view[written:]
Path(os.environ["FAKE_CAPTURE_STATE_FILE"]).write_text("pcm-written", encoding="utf-8")

# Keep the fake capture alive after one full window. The test intentionally
# closes the event writer instead; session cleanup must reap this process.
time.sleep(5)
"""


def write_executable(path: Path, source: str) -> Path:
  path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
  path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
  return path


def process_exists(pid: int) -> bool:
  try:
    os.kill(pid, 0)
  except ProcessLookupError:
    return False
  except PermissionError:
    return True
  return True


class ShutdownAfterCaptionWriter(captions.EventWriter):
  """Model the QML output owner disappearing after accepting a caption."""

  def emit(self, event: dict[str, object]) -> bool:
    accepted = super().emit(event)
    if accepted and event.get("type") == "caption":
      self.broken.set()
    return accepted


@unittest.skipUnless(
  os.name == "posix"
  and hasattr(os, "killpg")
  and hasattr(os, "getpgid")
  and hasattr(signal, "SIGTERM")
  and hasattr(signal, "SIGKILL"),
  "requires POSIX process groups and signals",
)
class WatchProcessIntegrationTests(unittest.TestCase):
  @unittest.skipUnless(
    sys.platform.startswith("linux") and shutil.which("setpriv"),
    "requires Linux setpriv parent-death signals",
  )
  def test_native_setpriv_parent_death_stops_the_owned_watcher(self) -> None:
    with tempfile.TemporaryDirectory() as temporary:
      work = Path(temporary)
      child_pid_file = work / "child.pid"
      child_ready_file = work / "child.ready"
      child_stopped_file = work / "child.stopped"
      worker = write_executable(work / "worker", """
        #!/usr/bin/env python3
        import os
        from pathlib import Path
        import signal
        import time

        stopped = Path(os.environ["CHILD_STOPPED"])
        def stop(_number, _frame):
          stopped.write_text("stopped", encoding="ascii")
          raise SystemExit(0)

        signal.signal(signal.SIGTERM, stop)
        Path(os.environ["CHILD_READY"]).write_text("ready", encoding="ascii")
        while True:
          time.sleep(0.05)
      """)
      owner = write_executable(work / "owner", """
        #!/usr/bin/env python3
        import os
        from pathlib import Path
        import subprocess
        import time

        child = subprocess.Popen([
          "setpriv", "--pdeathsig", "TERM", "--", os.environ["WORKER"]
        ])
        Path(os.environ["CHILD_PID"]).write_text(str(child.pid), encoding="ascii")
        deadline = time.monotonic() + 3
        ready = Path(os.environ["CHILD_READY"])
        while not ready.exists() and time.monotonic() < deadline:
          time.sleep(0.01)
        if not ready.exists():
          child.terminate()
          raise SystemExit("child did not become ready")
      """)
      environment = dict(os.environ)
      environment.update({
        "WORKER": str(worker),
        "CHILD_PID": str(child_pid_file),
        "CHILD_READY": str(child_ready_file),
        "CHILD_STOPPED": str(child_stopped_file),
      })

      owner_process = subprocess.run(
        [str(owner)],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
        timeout=5,
      )
      self.assertEqual(owner_process.returncode, 0, owner_process.stderr.decode())
      child_pid = int(child_pid_file.read_text(encoding="ascii"))
      deadline = time.monotonic() + 3
      while process_exists(child_pid) and time.monotonic() < deadline:
        time.sleep(0.01)
      self.assertFalse(process_exists(child_pid), child_pid)
      self.assertEqual(child_stopped_file.read_text(encoding="ascii"), "stopped")

  def test_real_subprocess_pipeline_captions_and_reaps_children(self) -> None:
    with tempfile.TemporaryDirectory() as temporary:
      work = Path(temporary).resolve()
      model = work / "ggml-base.en.bin"
      model.write_bytes(b"fake integration model")
      server = write_executable(work / "whisper-server", FAKE_WHISPER_SERVER)
      capture = write_executable(work / "pw-record", FAKE_PW_RECORD)
      setpriv = write_executable(work / "setpriv", FAKE_SETPRIV)
      server_pid_file = work / "server.pid"
      capture_pid_file = work / "capture.pid"
      health_file = work / "health"
      inference_file = work / "inference"
      capture_state_file = work / "capture-state"
      output = io.StringIO()
      writer = ShutdownAfterCaptionWriter(output)

      environment = {
        "XDG_RUNTIME_DIR": str(work),
        "FAKE_SERVER_PID_FILE": str(server_pid_file),
        "FAKE_CAPTURE_PID_FILE": str(capture_pid_file),
        "FAKE_HEALTH_FILE": str(health_file),
        "FAKE_INFERENCE_FILE": str(inference_file),
        "FAKE_CAPTURE_STATE_FILE": str(capture_state_file),
      }

      def executable(name: str) -> str:
        if name == "setpriv":
          return str(setpriv)
        raise AssertionError(f"unexpected executable lookup: {name}")

      # The production gate is Linux because the real recorder is PipeWire.
      # These portable fakes let POSIX CI exercise the same process lifecycle.
      with (
        mock.patch.dict(os.environ, environment),
        mock.patch.object(captions, "platform_supported", return_value=True),
        mock.patch.object(captions, "executable", side_effect=executable),
        mock.patch.object(captions.sys, "stdin", io.StringIO("")),
      ):
        result = captions.run_watch_session(
          model=model,
          source="microphone",
          language="en",
          writer=writer,
          server_binary=str(server),
          capture_binary=str(capture),
          startup_timeout=3.0,
          capture_timeout=2.0,
        )

      events = [json.loads(line) for line in output.getvalue().splitlines()]
      statuses = [event["state"] for event in events if event.get("type") == "status"]
      captions_seen = [event for event in events if event.get("type") == "caption"]

      self.assertEqual(result, 0)
      self.assertEqual(statuses, ["starting", "listening"])
      self.assertEqual([event["text"] for event in captions_seen], ["A deterministic integration caption."])
      self.assertFalse(any(event.get("type") == "error" for event in events))
      self.assertTrue(writer.broken.is_set())
      self.assertEqual(health_file.read_text(encoding="utf-8"), "healthy")
      self.assertEqual(inference_file.read_text(encoding="utf-8"), "wav")
      self.assertEqual(capture_state_file.read_text(encoding="utf-8"), "pcm-written")

      child_pids = (
        int(server_pid_file.read_text(encoding="utf-8")),
        int(capture_pid_file.read_text(encoding="utf-8")),
      )
      deadline = time.monotonic() + 1.0
      while any(process_exists(pid) for pid in child_pids) and time.monotonic() < deadline:
        time.sleep(0.01)
      self.assertTrue(all(not process_exists(pid) for pid in child_pids), child_pids)


if __name__ == "__main__":
  unittest.main()
