from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ManifestTests(unittest.TestCase):
  def setUp(self) -> None:
    self.manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))

  def test_manifest_is_one_namespaced_on_demand_overlay(self) -> None:
    self.assertEqual(self.manifest["schemaVersion"], 1)
    self.assertEqual(self.manifest["id"], "io.github.josephbriones.live-captions")
    self.assertEqual(self.manifest["kinds"], ["overlay"])
    self.assertNotIn("keepLoaded", self.manifest)
    self.assertEqual(self.manifest["entryPoints"], {"overlay": "LiveCaptions.qml"})

  def test_manifest_entry_points_are_safe_existing_relative_paths(self) -> None:
    for relative in self.manifest["entryPoints"].values():
      path = Path(relative)
      self.assertFalse(path.is_absolute())
      self.assertNotIn("..", path.parts)
      self.assertTrue((ROOT / path).is_file())

  def test_distribution_files_exist_and_repository_has_no_symlinks(self) -> None:
    for relative in ("README.md", "LICENSE", "PRIVACY.md", "SECURITY.md"):
      self.assertTrue((ROOT / relative).is_file(), relative)
    symlinks = [
      path.relative_to(ROOT)
      for path in ROOT.rglob("*")
      if path.is_symlink() and ".git" not in path.relative_to(ROOT).parts
    ]
    self.assertEqual(symlinks, [])


class QmlSafetyTests(unittest.TestCase):
  @classmethod
  def setUpClass(cls) -> None:
    cls.qml = (ROOT / "LiveCaptions.qml").read_text(encoding="utf-8")

  def test_quattro_lifecycle_is_present_without_required_host_properties(self) -> None:
    self.assertIn("function open(payloadJson)", self.qml)
    self.assertIn("function close()", self.qml)
    for name in ("omarchyPath", "shell", "manifest"):
      self.assertNotRegex(self.qml, rf"required\s+property\s+\w+\s+{name}\b")

  def test_caption_surface_is_click_through(self) -> None:
    self.assertIn('WlrLayershell.namespace: "live-captions-text"', self.qml)
    self.assertRegex(self.qml, r"mask:\s*Region\s*\{\s*\}")
    self.assertIn("WlrKeyboardFocus.None", self.qml)
    self.assertIn("ExclusionMode.Ignore", self.qml)

  def test_close_stops_the_owned_qml_process_directly(self) -> None:
    close_body = self.qml[self.qml.index("function close()") : self.qml.index("function dismiss()")]
    self.assertIn("watchProcess.running = false", close_body)
    self.assertNotIn("Quickshell.execDetached", close_body)

  def test_watcher_uses_quickshell_stdin_for_controls(self) -> None:
    self.assertIn("stdinEnabled: true", self.qml)
    run_action = self.qml[self.qml.index("function runAction(name)") : self.qml.index("function applyDoctor")]
    self.assertIn('watchProcess.write(name + "\\n")', run_action)
    self.assertNotIn("actionProcess", self.qml)

  def test_real_capture_has_no_payload_autostart_path(self) -> None:
    open_body = self.qml[self.qml.index("function open(payloadJson)") : self.qml.index("function close()")]
    self.assertNotIn("autostart", open_body.lower())
    begin_body = self.qml[self.qml.index("function beginCaptions()") : self.qml.index("function runAction")]
    self.assertIn("watchProcess.running = true", begin_body)

  def test_backend_derived_text_is_rendered_as_plain_text(self) -> None:
    protected_bindings = (
      "text: modelData.text",
      "text: CaptionModel.speakerLabel(modelData)",
      "text: root.setupDetails",
      "text: root.errorMessage",
    )
    for binding in protected_bindings:
      start = self.qml.index(binding)
      nearby = self.qml[start : start + 240]
      self.assertIn("textFormat: Text.PlainText", nearby, binding)

  def test_ipc_contract_is_stable(self) -> None:
    self.assertIn('target: "io.github.josephbriones.live-captions"', self.qml)
    for signature in (
      "function open(payloadJson: string): string",
      "function close(): string",
      "function start(): string",
      "function pause(): string",
      "function resume(): string",
      "function stop(): string",
      "function state(): string",
      "function ping(): string",
    ):
      self.assertIn(signature, self.qml)
    self.assertIn('if (root.demoMode) return "demo-running"', self.qml)
    self.assertGreaterEqual(self.qml.count('if (root.demoMode) return "demo-read-only"'), 3)

  def test_doctor_output_finishes_before_it_can_mark_ready(self) -> None:
    doctor = self.qml[self.qml.index("Process {\n    id: doctorProcess") : self.qml.index("Process {\n    id: watchProcess")]
    self.assertIn("onStreamFinished", doctor)
    self.assertIn("CaptionModel.parseCommandResult(text)", doctor)
    exit_handler = doctor[doctor.index("onExited: function(exitCode)") :]
    self.assertNotIn("doctorStdout.text", exit_handler)

  def test_no_shell_interpreter_is_used_by_qml(self) -> None:
    self.assertNotRegex(self.qml, re.compile(r"\b(?:bash|sh|zsh)\b\s*,"))
    self.assertNotIn("sh -c", self.qml)


if __name__ == "__main__":
  unittest.main()
