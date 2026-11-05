"""Prune only affected search chunks and existing mail warning metadata."""
from contextlib import closing
import hashlib
import json
import re
import sqlite3
import uuid

from .archive import atomic_write
from .compact_mail import digest_file
from .web import CollectionError


def verified(root,manifest,name):
    from .delete_transaction import target
    payload=target(root,name).read_bytes()
    if {'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}!=manifest['files'].get(name):
        raise CollectionError('삭제할 항목의 색인·상태 파일이 변경되었습니다. 파일 무결성을 확인하세요.')
    return payload


def save(prepared,manifest,name,payload):
    if isinstance(payload,str):payload=payload.encode('utf-8')
    atomic_write(prepared/name,payload)
    manifest['files'][name]={'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}


def update_search(root,prepared,manifest,plan_path,baseline,progress):
    from .search_page import VERSION,INDEX,script_json
    if manifest.get('browser_index')!=VERSION or 'search/parts.sqlite3' not in manifest['files']:
        raise CollectionError('새 프로그램의 보관함 열기로 검색 화면을 한 번 갱신한 뒤 삭제하세요.')
    config=json.loads(verified(root,manifest,'search/config.json'))
    name='search/parts.sqlite3'
    (prepared/'search').mkdir(parents=True,exist_ok=True)
    baseline.copy(baseline.raw_path(name),prepared/name,manifest['files'][name],lambda:None,lambda *a,**kw:None,relative=name)
    removed=[]
    with closing(sqlite3.connect(prepared/name)) as db:
        db.execute('PRAGMA temp_store=FILE')
        db.execute('PRAGMA secure_delete=ON')
        db.execute('ATTACH DATABASE ? AS selection',(str(plan_path),))
        expected=db.execute('SELECT count(*) FROM selection.selected').fetchone()[0]
        if db.execute('SELECT count(*) FROM parts p JOIN selection.selected s ON p.id=s.id').fetchone()[0]!=expected:
            raise CollectionError('검색 화면과 삭제 목록의 건수가 다릅니다. 보관함 열기로 화면을 갱신하세요.')
        chunks=[row[0] for row in db.execute('SELECT DISTINCT p.chunk FROM parts p JOIN selection.selected s ON p.id=s.id')]
        deleted=0
        for number,chunk in enumerate(chunks,1):
            if not re.fullmatch(r'search/part-[0-9]{6}\.js',chunk) or chunk not in config['chunks']:
                raise CollectionError('검색 파일의 위치를 확인하지 못했습니다.')
            payload=verified(root,manifest,chunk).decode('utf-8')
            if not payload.startswith('window.archiveChunk=') or not payload.endswith(';'):raise CollectionError('검색 파일 형식이 다릅니다.')
            rows=json.loads(payload[len('window.archiveChunk='):-1])
            kept=[]
            for row in rows:
                if not db.execute('SELECT 1 FROM selection.selected WHERE id=?',(row[0],)).fetchone():
                    kept.append(row)
                    continue
                deleted+=1
                if row[1]=='mail':
                    for box in config['mailboxes']:
                        if box['id']==row[6]:box['count']-=1
                else:
                    for item in config['approval_categories']:
                        if item['id'] in row[10]:item['count']-=1
            if kept:save(prepared,manifest,chunk,'window.archiveChunk='+script_json(kept)+';')
            else:
                config['chunks'].remove(chunk)
                manifest['files'].pop(chunk,None)
                removed.append(chunk)
            progress('검색 목록 갱신',number,len(chunks))
        if deleted!=expected:raise CollectionError('검색 파일과 삭제 목록이 일치하지 않습니다. 보관함 열기로 화면을 갱신하세요.')
        db.execute('DELETE FROM parts WHERE id IN (SELECT id FROM selection.selected)')
        db.commit()
    manifest['files'][name]=digest_file(prepared/name)
    config['counts']={key:manifest['counts'].get(key,0) for key in ('approval','mail')}
    config['mail_warning_count']=manifest.get('portable_mail',{}).get('warning_count',0)
    config['edit_revision']=uuid.uuid4().hex
    save(prepared,manifest,'search/config.json',json.dumps(config,ensure_ascii=False))
    save(prepared,manifest,'index.html',INDEX.replace('__ARCHIVE_CONFIG__',script_json(config)))
    manifest['browser_edit_revision']=config['edit_revision']
    return removed


def update_mail(root,prepared,manifest,plan,original):
    """Keep unselected payloads/status untouched; subtract selected mail states."""
    from .portable_mail import ISSUE_REPORT,ISSUES
    portable=manifest.get('portable_mail',{})
    removed_warnings=0
    deleted=0
    for identity, in plan.execute("SELECT id FROM selected WHERE kind='mail'"):
        state=json.loads(verified(root,original,'mail/offline/'+identity+'/state.json'))
        codes=state.get('issues',[])
        if not isinstance(codes,list) or any(code not in ISSUES for code in codes):raise CollectionError('메일 상태 기록의 형식이 다릅니다.')
        deleted+=1
        removed_warnings+=bool(codes)
        changes={'warning_count':bool(codes),'raw_only_count':state.get('raw_only') is True,
                 'source_only_count':state.get('source_only') is True,
                 'unavailable_attachment_count':state.get('unavailable_attachments',0)}
        for key,value in changes.items():
            if type(value) not in (int,bool) or value<0 or portable.get(key,0)<value:
                raise CollectionError('메일 상태 집계가 일치하지 않습니다. 기존 백업을 보존했습니다.')
            portable[key]=portable.get(key,0)-int(value)
    if not deleted:return []
    portable['count']=manifest['counts'].get('mail',0)
    if not portable.get('warning_count',0):
        portable.pop('issues_path',None)
        manifest['files'].pop(ISSUE_REPORT,None)
        removed=[ISSUE_REPORT] if ISSUE_REPORT in original['files'] else []
    else:
        removed=[]
        if removed_warnings:
            # Reports contain one bounded generated entry per warning mail.
            # Read only report metadata; never reopen other message payloads.
            report=verified(root,manifest,ISSUE_REPORT).decode('utf-8')
            expression=re.compile(r'<li><a href="offline/(mail-[0-9]{9})/index\.html">.*?</ul></li>',re.S)
            skipped=0
            def keep(match):
                nonlocal skipped
                if plan.execute('SELECT 1 FROM selected WHERE id=?',(match[1],)).fetchone():
                    skipped+=1
                    return ''
                return match[0]
            report=expression.sub(keep,report)
            if skipped!=removed_warnings:raise CollectionError('메일 확인 목록의 건수가 일치하지 않습니다.')
            save(prepared,manifest,ISSUE_REPORT,report)
    manifest['warnings']=[value for value in manifest.get('warnings',[]) if not value.startswith('메일 바로보기 확인 필요:')]
    if portable.get('warning_count',0):
        manifest['warnings'].append('메일 바로보기 확인 필요: %d건 (원본 EML만 보존한 메일 %d건, 첨부 미확보 %d개). mail/processing-issues.html을 확인하세요.'%
            (portable['warning_count'],portable.get('raw_only_count',0),portable.get('unavailable_attachment_count',0)))
    return removed
