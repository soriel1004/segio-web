"""Bounded retries for Windows sharing violations, without changing permissions."""
import os
import time
from contextlib import contextmanager


def retry_file_io(action, checkpoint=lambda: None, operation='file_io'):
    for attempt in range(5):
        checkpoint()
        try:
            return action()
        except OSError as exc:
            # Access denied alone (5/EACCES) can mean a permanent policy or
            # permissions failure. Retry only explicit Windows file locks.
            if getattr(exc, 'winerror', None) not in (32, 33) or attempt == 4:
                exc.local_operation = operation
                raise
            time.sleep(.1 * 2**attempt)


def replace_file(source, destination, checkpoint=lambda: None):
    return retry_file_io(lambda:os.replace(source,destination),checkpoint,'replace')


def remove_file(path, checkpoint=lambda: None):
    return retry_file_io(lambda:os.unlink(path),checkpoint)


MERGE_OPERATIONS = {'stage_incoming_mail','inspect_previous','create_work_folder','link_previous',
                    'copy_previous','read_previous','replace_mail','discard_overlap','save_metadata','commit_backup'}
MERGE_KINDS = {'mail_raw','mail_sources','mail_attachment','mail_page','approval_document','catalog','search_database',
               'search_chunk','manifest','metadata'}


def merge_file_kind(name):
    name=str(name or '')
    if name.startswith('mail/raw/'):return 'mail_raw'
    if name.startswith('mail/offline/'):
        return 'mail_attachment' if '/attachments/' in name else 'mail_page'
    if name.startswith('documents/'):return 'approval_document'
    if name.startswith('search/'):return 'search_chunk'
    return {'catalog.json':'catalog','catalog.sqlite3':'search_database','manifest.json':'manifest',
            'mail/sources.json':'mail_sources'}.get(name,'metadata')


@contextmanager
def merge_io(operation, name=''):
    try:
        yield
    except OSError as exc:
        if not hasattr(exc,'merge_diagnostic'):
            exc.merge_diagnostic={'operation':operation,'file_kind':merge_file_kind(name)}
        raise


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
