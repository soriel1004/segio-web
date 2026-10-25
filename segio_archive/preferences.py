"""Remember only the user's output-folder preference, never credentials."""
import json
import os
from pathlib import Path

from .archive import atomic_write

PROTOCOL = 'segio-ui-settings/1'


def settings_path():
    base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
    if base:
        return Path(base)/'SEGIOArchive'/'settings.json'
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home()/'.config')/'SEGIOArchive'/'settings.json'


def valid_folder(value):
    return isinstance(value,str) and bool(value.strip()) and len(value)<=32767 and not any(ord(c)<32 for c in value)


def load_output_folder(default):
    try:
        path=settings_path()
        if path.stat().st_size>65536:
            return str(default)
        data=json.loads(path.read_bytes())
        if data.get('protocol')==PROTOCOL and valid_folder(data.get('output_folder')):
            return data['output_folder']
    except (OSError,ValueError,TypeError,AttributeError):
        pass
    return str(default)


def save_output_folder(value):
    if not valid_folder(value):
        return False
    try:
        folder=str(Path(value.strip()).expanduser().absolute())
        atomic_write(settings_path(),json.dumps({'protocol':PROTOCOL,'output_folder':folder},ensure_ascii=False).encode('utf-8'))
        return True
    except (OSError,ValueError):
        return False
