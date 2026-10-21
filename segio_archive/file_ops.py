"""Bounded retries for Windows sharing violations, without changing permissions."""
import os
import time


def replace_file(source, destination, checkpoint=lambda: None):
    for attempt in range(5):
        checkpoint()
        try:
            return os.replace(source, destination)
        except OSError as exc:
            # Access denied alone (5/EACCES) can mean a permanent policy or
            # permissions failure. Retry only explicit Windows file locks.
            if getattr(exc, 'winerror', None) not in (32, 33) or attempt == 4:
                exc.local_operation = 'replace'
                raise
            time.sleep(.1 * 2**attempt)


def file_diagnostic(exception):
    if not isinstance(exception, OSError):
        return None
    types = {'PermissionError', 'FileNotFoundError', 'FileExistsError', 'OSError',
             'IsADirectoryError', 'NotADirectoryError'}
    kind = type(exception).__name__
    return {'exception_type': kind if kind in types else 'OSError',
            'errno': exception.errno if type(exception.errno) is int else None,
            'winerror': getattr(exception, 'winerror', None) if type(getattr(exception, 'winerror', None)) is int else None,
            'operation': 'replace' if getattr(exception, 'local_operation', None) == 'replace' else 'file_io'}
