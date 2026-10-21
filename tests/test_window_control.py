import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from segio_archive.browser import BrowserChoice, launch_browser
from segio_archive.window_control import minimize_browser_windows, minimize_console


class WindowControlTests(unittest.TestCase):
    def test_browser_minimize_targets_only_unique_windows_of_the_given_context(self):
        pages=[Mock(),Mock(),Mock()]
        for page in pages:page.is_closed.return_value=False
        context=Mock(pages=pages)
        sessions=[Mock(),Mock(),Mock()]
        context.new_cdp_session.side_effect=sessions
        for session,number in zip(sessions,(12,12,13)):
            session.send.side_effect=lambda command,*_,number=number: {'windowId':number}
        self.assertTrue(minimize_browser_windows(context))
        for index,number in ((0,12),(2,13)):
            sessions[index].send.assert_any_call('Browser.setWindowBounds',{'windowId':number,'bounds':{'windowState':'minimized'}})
        self.assertEqual(sessions[1].send.call_count,1)
        for session in sessions:session.detach.assert_called_once()

    def test_failed_or_closed_browser_window_does_not_stop_backup_or_log_private_error(self):
        closed,open_page=Mock(),Mock()
        closed.is_closed.return_value=True
        open_page.is_closed.return_value=False
        session=Mock()
        session.send.side_effect=RuntimeError('PRIVATE_SESSION_URL_PASSWORD')
        context=Mock(pages=[closed,open_page])
        context.new_cdp_session.return_value=session
        logs=[]
        self.assertFalse(minimize_browser_windows(context,logs.append))
        self.assertEqual(context.new_cdp_session.call_count,1)
        session.detach.assert_called_once()
        self.assertNotIn('PRIVATE',' '.join(logs))

    def test_minimized_browser_launch_keeps_headed_window_and_skips_flag_in_headless_mode(self):
        chromium=Mock()
        with patch('segio_archive.browser.browser_candidates',return_value=[BrowserChoice('browser','Browser')]):
            launch_browser(chromium,headless=False,minimize=True)
            chromium.launch.assert_called_with(headless=False,executable_path='browser',args=['--start-minimized'])
            launch_browser(chromium,headless=True,minimize=True)
            chromium.launch.assert_called_with(headless=True,executable_path='browser')

    def test_console_uses_own_handle_and_minimize_instead_of_hide(self):
        kernel,user=Mock(),Mock()
        kernel.GetConsoleWindow.return_value=123
        with patch('segio_archive.window_control.sys.platform','win32'),patch('ctypes.WinDLL',create=True,side_effect=[kernel,user]):
            self.assertTrue(minimize_console())
        user.ShowWindow.assert_called_once_with(123,6)
        kernel.GetConsoleWindow.return_value=None
        user.reset_mock()
        with patch('segio_archive.window_control.sys.platform','win32'),patch('ctypes.WinDLL',create=True,side_effect=[kernel,user]):
            self.assertFalse(minimize_console())
        user.ShowWindow.assert_not_called()

    @unittest.skipUnless(sys.platform=='win32' and os.environ.get('SEGIO_TEST_BROWSER'),'Native Windows browser required')
    def test_native_browser_window_reports_minimized_and_can_be_restored(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=False,executable_path=os.environ['SEGIO_TEST_BROWSER'])
            try:
                context=browser.new_context()
                page=context.new_page()
                self.assertTrue(minimize_browser_windows(context))
                session=context.new_cdp_session(page)
                identity=session.send('Browser.getWindowForTarget')['windowId']
                for _ in range(40):
                    if session.send('Browser.getWindowBounds',{'windowId':identity})['bounds']['windowState']=='minimized':break
                    page.wait_for_timeout(50)
                self.assertEqual(session.send('Browser.getWindowBounds',{'windowId':identity})['bounds']['windowState'],'minimized')
                session.send('Browser.setWindowBounds',{'windowId':identity,'bounds':{'windowState':'normal'}})
                for _ in range(40):
                    if session.send('Browser.getWindowBounds',{'windowId':identity})['bounds']['windowState']=='normal':break
                    page.wait_for_timeout(50)
                self.assertEqual(session.send('Browser.getWindowBounds',{'windowId':identity})['bounds']['windowState'],'normal')
                session.detach()
            finally:browser.close()


if __name__=='__main__':unittest.main()
