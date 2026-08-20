from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ManifestTests(unittest.TestCase):
  def setUp(self) -> None:
    self.manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))

  def test_manifest_is_one_namespaced_resident_overlay(self) -> None:
    self.assertEqual(self.manifest["schemaVersion"], 1)
    self.assertEqual(self.manifest["id"], "io.github.josephbriones.live-captions")
    self.assertEqual(self.manifest["version"], "0.2.0")
    self.assertEqual(self.manifest["kinds"], ["overlay"])
    self.assertIs(self.manifest["keepLoaded"], True)
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

  def test_ci_pins_actions_and_runs_arch_omarchy_contract(self) -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    revisions = re.findall(r"uses:\s+[^@\s]+@([^\s#]+)", workflow)
    self.assertGreaterEqual(len(revisions), 5)
    for revision in revisions:
      self.assertRegex(revision, r"^[0-9a-f]{40}$")
    for contract in (
      'python-version: ["3.12", "3.14"]',
      "container: archlinux:latest",
      "git jq quickshell qt6-declarative",
      "omarchy-plugin-validate",
      'ln -s "$OMARCHY_PATH/shell" "$RUNNER_TEMP/live-captions-qml/qs"',
      "/usr/lib/qt6/bin/qmllint",
      "945549699026df6c888a6b1bd4e06fbf55a67595",
    ):
      self.assertIn(contract, workflow)


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
    caption_surface = self.qml[
      self.qml.index("PanelWindow {\n      id: captionWindow")
      : self.qml.index("// One small interactive card")
    ]
    self.assertIn('WlrLayershell.namespace: "live-captions-text"', caption_surface)
    self.assertRegex(caption_surface, r"mask:\s*Region\s*\{\s*\}")
    self.assertIn("WlrKeyboardFocus.None", caption_surface)
    self.assertIn("ExclusionMode.Ignore", caption_surface)
    self.assertNotIn("AccessButton", caption_surface)

  def test_control_surface_uses_omarchy_keyboard_focus_lifecycle(self) -> None:
    controls = self.qml[self.qml.index('WlrLayershell.namespace: "live-captions-controls"') :]
    self.assertIn("WlrKeyboardFocus.Exclusive", controls)
    self.assertIn("WlrKeyboardFocus.OnDemand", controls)
    self.assertIn("WlrKeyboardFocus.None", controls)
    self.assertIn("interval: 75", controls)
    self.assertIn("expandButton.forceActiveFocus()", self.qml)
    self.assertIn("controlWindow.primeKeyboardFocus()", self.qml)
    self.assertIn("Keys.onEscapePressed", controls)
    self.assertIn("mask: Region { item: controlCard }", controls)

  def test_every_control_button_is_keyboard_and_at_accessible(self) -> None:
    controls = self.qml[self.qml.index('WlrLayershell.namespace: "live-captions-controls"') :]
    self.assertNotRegex(controls, r"(?m)^\s+Button\s*\{")
    self.assertEqual(controls.count("AccessButton {"), 16)
    self.assertIn("focusable: true", self.qml)
    self.assertIn("Accessible.onPressAction", self.qml)
    self.assertIn("Accessible.role: Accessible.Dialog", controls)
    self.assertGreaterEqual(controls.count("Accessible.role: Accessible.RadioButton"), 4)

  def test_close_stops_the_owned_qml_process_directly(self) -> None:
    close_body = self.qml[self.qml.index("function close()") : self.qml.index("function dismiss()")]
    self.assertIn("watchProcess.running = false", close_body)
    self.assertIn("segments = []", close_body)
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
    self.assertIn('if (root.demoMode) return watchProcess.running ? "demo-running" : "demo-finished"', self.qml)
    self.assertGreaterEqual(self.qml.count('if (root.demoMode) return "demo-read-only"'), 3)

  def test_doctor_output_finishes_before_it_can_mark_ready(self) -> None:
    doctor = self.qml[self.qml.index("Process {\n    id: doctorProcess") : self.qml.index("Process {\n    id: watchProcess")]
    self.assertIn("onStreamFinished", doctor)
    self.assertIn("CaptionModel.parseCommandResult(text)", doctor)
    exit_handler = doctor[doctor.index("onExited: function(exitCode)") :]
    self.assertNotIn("doctorStdout.text", exit_handler)

  def test_process_start_failures_have_qml_fallbacks(self) -> None:
    doctor = self.qml[self.qml.index("Process {\n    id: doctorProcess") : self.qml.index("Process {\n    id: watchProcess")]
    watcher = self.qml[self.qml.index("Process {\n    id: watchProcess") : self.qml.index("IpcHandler {")]
    for block in (doctor, watcher):
      self.assertIn("onStarted:", block)
      self.assertIn("onRunningChanged:", block)
      self.assertIn("Could not start the local caption helper", block)

  def test_closed_or_stopping_sessions_reject_stale_events(self) -> None:
    apply_event = self.qml[self.qml.index("function applyEvent(event)") : self.qml.index("Process {\n    id: doctorProcess")]
    self.assertIn("if (!opened || explicitStop || !event || !event.valid) return", apply_event)
    self.assertIn("reopenPending", self.qml)
    self.assertIn("resumePendingOpen", self.qml)

  def test_doctor_source_only_overrides_the_payload_default(self) -> None:
    self.assertIn("sourceExplicit = options.sourceExplicit", self.qml)
    apply_doctor = self.qml[self.qml.index("function applyDoctor(event)") : self.qml.index("function applyEvent(event)")]
    self.assertIn('if (!sourceExplicit && event.source && event.source !== "unknown")', apply_doctor)

  def test_demo_is_single_source_and_settles_idle(self) -> None:
    self.assertIn('? [root.backendPath, "watch", "--demo", "--source", root.activeSource]', self.qml)
    self.assertIn("if (!demoMode && event.source", self.qml)
    self.assertIn("source: activeSource", self.qml)
    watcher = self.qml[self.qml.index("Process {\n    id: watchProcess") : self.qml.index("IpcHandler {")]
    self.assertRegex(watcher, r"if \(root\.demoMode\)[\s\S]*?else \{\s*root\.phase = \"idle\"")

  def test_stop_privacy_and_ipc_follow_the_process(self) -> None:
    self.assertIn('if (watchProcess.running) return sourceText + " session active', self.qml)
    self.assertGreaterEqual(self.qml.count("visible: !root.demoMode && watchProcess.running"), 2)
    stop = self.qml[self.qml.index("function stop(): string") : self.qml.index("function state(): string")]
    self.assertIn('if (!watchProcess.running) return "not-recording"', stop)
    self.assertIn("running: watchProcess.running", self.qml)

  def test_setup_failure_offers_a_demo(self) -> None:
    self.assertIn('text: "Try demo"', self.qml)
    self.assertIn("onClicked: root.open(JSON.stringify({", self.qml)

  def test_no_shell_interpreter_is_used_by_qml(self) -> None:
    self.assertNotRegex(self.qml, re.compile(r"\b(?:bash|sh|zsh)\b\s*,"))
    self.assertNotIn("sh -c", self.qml)


if __name__ == "__main__":
  unittest.main()
