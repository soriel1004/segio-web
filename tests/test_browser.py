import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from segio_archive.browser import BrowserChoice, BrowserUnavailable, browser_candidates, launch_browser


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.registry = patch("segio_archive.browser.registry_paths", return_value=[])
        self.registry_mock = self.registry.start()

    def tearDown(self):
        self.registry.stop()
        self.tmp.cleanup()

    def executable(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return str(path)

    def test_windows_uses_installed_edge_before_chrome_and_bundle(self):
        edge = self.executable("Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
        chrome = self.executable("Local Data/Google/Chrome/Application/chrome.exe")
        bundled = self.executable("bundled/chrome.exe")
        found = browser_candidates(bundled, platform="win32", environ={
            "PROGRAMFILES(X86)": str(self.root / "Program Files (x86)"),
            "LOCALAPPDATA": str(self.root / "Local Data")})
        self.assertEqual([c.executable for c in found], [edge, chrome, bundled])

    def test_custom_registry_installation_is_found_and_deduplicated(self):
        edge = self.executable("custom/Edge/msedge.exe")
        self.registry_mock.side_effect = lambda name: [edge, edge] if name == "msedge.exe" else []
        found = browser_candidates(str(self.root / "absent"), platform="win32", environ={})
        self.assertEqual(found, [BrowserChoice(edge, "Microsoft Edge")])

    def test_existing_bundle_still_works_without_installed_browser(self):
        bundled = self.executable("bundled/chrome.exe")
        self.assertEqual(browser_candidates(bundled, platform="win32", environ={}),
                         [BrowserChoice(bundled, "전용 Chromium")])

    def test_explicit_executable_does_not_fall_back_silently(self):
        with self.assertRaises(BrowserUnavailable):
            browser_candidates("unused", str(self.root / "missing.exe"))
        executable = self.executable("chosen/chrome.exe")
        self.assertEqual(browser_candidates("unused", executable)[0].executable, executable)

    def test_missing_browser_has_actionable_error_without_download(self):
        chromium = Mock()
        chromium.executable_path = str(self.root / "missing")
        with patch("segio_archive.browser.browser_candidates", return_value=[]):
            with self.assertRaisesRegex(BrowserUnavailable, "Microsoft Edge.*Google Chrome"):
                launch_browser(chromium, headless=True)
        chromium.launch.assert_not_called()

    def test_launch_failure_tries_next_browser_with_fresh_launch(self):
        chromium = Mock()
        browser = object()
        chromium.launch.side_effect = [RuntimeError("failed"), browser]
        choices = [BrowserChoice("edge", "Edge"), BrowserChoice("chrome", "Chrome")]
        with patch("segio_archive.browser.browser_candidates", return_value=choices):
            self.assertIs(launch_browser(chromium, headless=False), browser)
        self.assertEqual(chromium.launch.call_count, 2)
        self.assertEqual(chromium.launch.call_args.kwargs,
                         {"headless": False, "executable_path": "chrome"})

    def test_all_launch_failures_do_not_expose_browser_diagnostics(self):
        chromium = Mock()
        chromium.launch.side_effect = RuntimeError("sensitive internal diagnostic")
        with patch("segio_archive.browser.browser_candidates", return_value=[BrowserChoice("edge", "Edge")]):
            with self.assertRaises(BrowserUnavailable) as caught:
                launch_browser(chromium, headless=True)
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertIn("install_browser_windows.bat", str(caught.exception))


@unittest.skipUnless(sys.platform == "win32", "Windows desktop test; Linux has no display")
class WindowsStartupTests(unittest.TestCase):
    def test_login_window_opens_without_browser_download_or_launch(self):
        from segio_archive.gui import App, APP_TITLE, DB_MODE
        with patch("segio_archive.browser.launch_browser") as launch:
            app = App()
            try:
                app.update_idletasks()
                app.update()  # Process native Windows map events before visibility checks.
                self.assertTrue(app.winfo_exists())
                self.assertTrue(app.title().startswith(APP_TITLE))
                self.assertEqual(app.user.get(), "")
                self.assertEqual(app.password.get(), "")
                self.assertEqual(app.method.get(), DB_MODE)
                self.assertIn('192.168.0.100',app.server.get())
                self.assertFalse(hasattr(app,'method_box'))
                self.assertTrue(app.approvals.get())
                self.assertTrue(app.mail.get())
                self.assertFalse(app.pdf.get())
                self.assertTrue(app.http.get())
                self.assertFalse(app.manual.get())
                self.assertTrue(app.web_form.winfo_ismapped())
                def descendants(widget):
                    return [child for child in widget.winfo_children() for child in [child]+descendants(child)]
                labels=[str(widget.cget('text')) for widget in descendants(app) if widget.winfo_class()=='TLabel']
                self.assertIn('그룹웨어 ID',labels)
                self.assertIn('그룹웨어 암호',labels)
                self.assertNotIn('가져오기 방식',labels)
                self.assertFalse(app.running)
                launch.assert_not_called()
            finally:
                app.close()
