"""Discover installed browsers without downloading anything or using personal profiles."""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


class BrowserUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class BrowserChoice:
    executable: str
    label: str


def registry_paths(executable: str):
    """Windows App Paths also covers browser installations in custom locations."""
    try:
        import winreg
    except ImportError:
        return []
    paths = []
    key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as handle:
                    value = winreg.QueryValueEx(handle, None)[0]
                if isinstance(value, str):
                    paths.append(os.path.expandvars(value.strip().strip('"')))
            except OSError:
                pass
    return paths


def browser_candidates(bundled_path: str, explicit: str | None = None,
                       *, platform: str | None = None, environ=None) -> list[BrowserChoice]:
    if explicit:
        if not Path(explicit).is_file():
            raise BrowserUnavailable("지정한 브라우저 실행 파일을 찾지 못했습니다.")
        return [BrowserChoice(str(explicit), "지정한 브라우저")]
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    options = []
    if platform == "win32":
        roots = [environ.get("PROGRAMFILES"), environ.get("PROGRAMFILES(X86)"),
                 environ.get("LOCALAPPDATA")]
        for exe, subpath, label in (
            ("msedge.exe", "Microsoft/Edge/Application/msedge.exe", "Microsoft Edge"),
            ("chrome.exe", "Google/Chrome/Application/chrome.exe", "Google Chrome"),
        ):
            options.extend((p, label) for p in registry_paths(exe))
            options.extend((str(Path(root) / subpath), label) for root in roots if root)
    else:
        for name, label in (("chromium", "Chromium"), ("chromium-browser", "Chromium"),
                            ("google-chrome", "Google Chrome"), ("microsoft-edge", "Microsoft Edge")):
            path = shutil.which(name)
            if path:
                options.append((path, label))
    options.append((bundled_path, "전용 Chromium"))
    seen = set()
    result = []
    for executable, label in options:
        path = Path(executable)
        key = str(path.resolve()).casefold()
        if key not in seen and path.is_file():
            seen.add(key)
            result.append(BrowserChoice(str(path), label))
    return result


def launch_browser(chromium, *, headless: bool, explicit: str | None = None, log=lambda _: None, minimize=False):
    candidates = browser_candidates(chromium.executable_path, explicit)
    if not candidates:
        raise BrowserUnavailable(
            "실행할 브라우저를 찾지 못했습니다. Microsoft Edge 또는 Google Chrome을 설치한 뒤 "
            "다시 수집하세요. 전용 Chromium은 install_browser_windows.bat으로 선택 설치할 수 있습니다.")
    for choice in candidates:
        try:
            options = {'headless':headless, 'executable_path':choice.executable}
            if minimize and not headless:
                options['args'] = ['--start-minimized']
            browser = chromium.launch(**options)
            log(f"{choice.label} 사용 · 개인 브라우저 프로필과 분리된 창을 엽니다.")
            return browser
        except Exception:
            log(f"{choice.label} 실행 실패. 다른 설치된 브라우저를 확인합니다.")
    raise BrowserUnavailable(
        "설치된 브라우저를 실행하지 못했습니다. Edge 또는 Chrome을 업데이트하고 다시 수집하세요. "
        "전용 Chromium을 사용하려면 install_browser_windows.bat을 실행하세요.")
