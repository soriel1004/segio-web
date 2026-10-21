"""Minimize only this application's console and automation browser windows."""
import sys


def minimize_console():
    if sys.platform != 'win32':
        return False
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        user = ctypes.WinDLL('user32', use_last_error=True)
        kernel.GetConsoleWindow.restype = wintypes.HWND
        user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        handle = kernel.GetConsoleWindow()
        if not handle:
            return False  # Windowed EXE has no console.
        user.ShowWindow(handle, 6)  # SW_MINIMIZE; the window stays on the taskbar.
        return True
    except (AttributeError, OSError):
        return False


def minimize_browser_windows(context, log=lambda _: None):
    """CDP identifies our own windows without searching by process/title."""
    minimized = set()
    for page in context.pages:
        session = None
        try:
            if page.is_closed():
                continue
            session = context.new_cdp_session(page)
            window = session.send('Browser.getWindowForTarget')
            identity = window['windowId']
            if identity not in minimized:
                session.send('Browser.setWindowBounds', {
                    'windowId': identity, 'bounds': {'windowState': 'minimized'}})
                minimized.add(identity)
        except Exception:
            log('웹 로그인 창을 자동 최소화하지 못했습니다. 수집은 계속합니다.')
        finally:
            if session is not None:
                try:
                    session.detach()
                except Exception:
                    pass
    return bool(minimized)
