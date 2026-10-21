"""Disk checks for large archives; sizes are Python integers, including ZIP64."""
import shutil
from .web import CollectionError

RESERVE = 64 * 1024 * 1024


def require_space(path, needed, phase):
    free = shutil.disk_usage(path).free
    if free < needed + RESERVE:
        raise CollectionError('%s 저장 공간이 부족합니다. 필요 여유 %.1f GiB / 현재 %.1f GiB. 공간을 확보하거나 다른 드라이브를 선택하세요. [DISK_FULL]' %
                              (phase, (needed+RESERVE)/1024**3, free/1024**3))
