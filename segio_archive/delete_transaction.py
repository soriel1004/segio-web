"""Commit only changed metadata and selected paths, with durable rollback."""
from contextlib import closing
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3

from .archive import atomic_write
from .compact_mail import digest_file
from .file_ops import replace_file
from .web import CollectionError

PROTOCOL='segio-delete-transaction/1'


def sync_directory(path):
    if os.name=='nt':return
    descriptor=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


def write_record(path,data):
    atomic_write(path,json.dumps(data).encode())
    with path.open('r+b') as output:os.fsync(output.fileno())
    sync_directory(path.parent)


def journal_path(root):
    return root.parent/('.segio-delete-'+root.name+'.json')


def target(root,name):
    from .db_client import existing_member
    existing_member(name)
    path=root/name
    if root.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or any(p.is_symlink() for p in (path,*path.parents) if p!=root and p.is_relative_to(root)):
        raise CollectionError('삭제 반영 경로를 확인하지 못했습니다. 백업을 보존하고 중단했습니다.')
    return path


def valid_operation(name,kind):
    if kind=='delete':
        return bool(re.fullmatch(r'(?:documents/[A-Za-z0-9_-]+|mail/offline/mail-[0-9]{9}|mail/raw/[0-9]{6}\.(?:bin|eml|mbox)|mail/processing-issues\.html|search/part-[0-9]{6}\.js)',name))
    return kind=='write' and (name in ('manifest.json','catalog.json','catalog.sqlite3','local-deletions.jsonl',
        'mail/sources.json','mail/processing-issues.html','index.html','index.bat','search/config.json','search/parts.sqlite3') or
        bool(re.fullmatch(r'search/part-[0-9]{6}\.js',name)))


def recover(root):
    """Caller holds the backup update lock. No intact payload is copied/read."""
    path=journal_path(root)
    if not path.exists():return []
    if path.is_symlink() or path.stat().st_size>65536:raise CollectionError('삭제 반영 확인 기록을 확인하세요.')
    data=json.loads(path.read_bytes())
    name=data.get('stage','')
    if data.get('protocol')!=PROTOCOL or not re.fullmatch(r'\.segio-delete-[A-Za-z0-9_-]+',name):
        raise CollectionError('삭제 반영 확인 기록의 형식이 다릅니다.')
    stage=root.parent/name
    finalized=data.get('committed') is True or data.get('rolled_back') is True
    if stage.is_symlink():raise CollectionError('삭제 복구 폴더의 경로를 확인하세요.')
    if not stage.is_dir():
        if finalized:
            path.unlink()
            sync_directory(root.parent)
            return []
        raise CollectionError('삭제 복구 폴더를 확인하지 못했습니다. 기존 폴더를 보존하세요.')
    plan=target(stage,'operations.sqlite3')
    committed=data.get('committed') is True
    if not finalized:
        if digest_file(plan)!=data.get('operations'):raise CollectionError('삭제 복구 작업 목록이 변경되었습니다.')
        if not (root/'manifest.json').exists() and target(stage/'old','manifest.json').is_file():
            replace_file(stage/'old'/'manifest.json',root/'manifest.json')
        current=digest_file(target(root,'manifest.json'))
        if current==data.get('after'):
            committed=True
            data['committed']=True
            write_record(path,data)
        elif current!=data.get('before'):
            raise CollectionError('삭제 도중 백업 검증 목록이 변경되었습니다. 복구 폴더를 보존하세요.')
    if not committed and not data.get('rolled_back'):
        with closing(sqlite3.connect(plan.as_uri()+'?mode=ro',uri=True)) as db:
            for name,kind,present in db.execute('SELECT name,kind,present FROM operations ORDER BY ordinal DESC'):
                if not valid_operation(name,kind):raise CollectionError('삭제 복구 경로 형식이 다릅니다.')
                old=target(stage/'old',name)
                destination=target(root,name)
                if old.exists():
                    if kind=='delete' and destination.exists():raise CollectionError('삭제 복구 위치에 다른 파일이 생겼습니다. 두 폴더를 보존하세요.')
                    destination.parent.mkdir(parents=True,exist_ok=True)
                    replace_file(old,destination)
                elif not present and kind=='write' and destination.exists():
                    # A new metadata file is ours only if it was moved out of
                    # the prepared set; leave unrelated pre-existing files alone.
                    if not target(stage/'new',name).exists():destination.unlink()
                elif present and not destination.exists():
                    raise CollectionError('삭제 복구에 필요한 원본 파일을 확인하지 못했습니다.')
        data['rolled_back']=True
        write_record(path,data)
    try:
        shutil.rmtree(stage)
        path.unlink()
        sync_directory(root.parent)
    except OSError:
        if not committed:raise
        return ['목록 삭제는 완료했지만 임시 보관한 삭제 파일의 정리를 확인하지 못했습니다.']
    return []


def commit(root,stage,removed,before,progress=lambda *args:None):
    """Prepare a small immutable operation list before touching the backup."""
    if journal_path(root).exists():raise CollectionError('이전 삭제 작업의 임시 파일 정리를 먼저 확인하세요.')
    # Flush only the small changed metadata set, not the untouched mailbox.
    directories={stage,stage/'new',root,root.parent}
    for path in (stage/'new').rglob('*'):
        if path.is_file():
            with path.open('r+b') as output:os.fsync(output.fileno())
        elif path.is_dir():directories.add(path)
    for path in directories:sync_directory(path)
    plan=stage/'operations.sqlite3'
    with closing(sqlite3.connect(plan)) as db:
        db.execute('CREATE TABLE operations(ordinal INTEGER PRIMARY KEY,name TEXT UNIQUE,kind TEXT,present INTEGER)')
        def add(name,kind):
            if not valid_operation(name,kind):raise CollectionError('삭제 반영 파일 형식이 다릅니다.')
            path=target(root,name)
            db.execute('INSERT INTO operations(name,kind,present) VALUES (?,?,?)',(name,kind,int(path.exists())))
        for name in removed:add(name,'delete')
        for path in sorted((stage/'new').rglob('*')):
            if path.is_file() and path.name!='manifest.json':add(path.relative_to(stage/'new').as_posix(),'write')
        add('manifest.json','write')  # The manifest is the commit point, always last.
        db.commit()
    if digest_file(root/'manifest.json')!=before:raise CollectionError('선택 확인 후 백업이 변경되었습니다. 목록을 다시 여세요.')
    data={'protocol':PROTOCOL,'stage':stage.name,'before':before,'after':digest_file(stage/'new'/'manifest.json'),
          'operations':digest_file(plan),'committed':False}
    write_record(journal_path(root),data)
    try:
        with closing(sqlite3.connect(plan.as_uri()+'?mode=ro',uri=True)) as db:
            total=db.execute('SELECT count(*) FROM operations').fetchone()[0]
            for position,(name,kind,present) in enumerate(db.execute('SELECT name,kind,present FROM operations ORDER BY ordinal'),1):
                if name=='manifest.json':
                    for parent in directories:
                        if parent.exists():sync_directory(parent)
                destination=target(root,name)
                old=target(stage/'old',name)
                if present:
                    old.parent.mkdir(parents=True,exist_ok=True)
                    replace_file(destination,old)
                    directories.update((destination.parent,old.parent,stage/'old'))
                if kind=='write':
                    destination.parent.mkdir(parents=True,exist_ok=True)
                    replace_file(target(stage/'new',name),destination)
                    directories.add(destination.parent)
                if position%100==0 or position==total:progress('삭제 반영',position,total)
    except Exception:
        try:recover(root)
        except Exception:
            raise CollectionError('삭제 반영을 완료하지 못했습니다. 원본은 복구 폴더에 보존했습니다. 새 프로그램의 보관함 열기로 복구를 확인하세요.') from None
        raise
    data['committed']=True
    try:
        for parent in directories:
            if parent.exists():sync_directory(parent)
        write_record(journal_path(root),data)
    except OSError:
        return ['목록 삭제는 완료했지만 임시 보관 파일의 정리 기록을 저장하지 못했습니다.']
    return recover(root)
