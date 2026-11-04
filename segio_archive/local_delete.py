"""Transactional edits to one completed personal backup, with disk-backed selection."""
from contextlib import closing
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tempfile
import time

from .archive import atomic_write
from .compact_mail import digest_file, search_filter
from .date_incremental import DateBaseline, update_lock
from .web import CollectionError

TOMBSTONES='local-deletions.jsonl'
PROTOCOL='segio-local-deletion/1'
BATCH=500


def approval_key(identity):
    return hashlib.sha256(identity.encode('utf-8')).hexdigest()


def mail_key(stream, name_hex, begin, end):
    if type(begin) is not int or type(end) is not int or not 0<=begin<=end<=os.fstat(stream.fileno()).st_size:
        raise CollectionError('메일 원본의 저장 위치·크기를 확인하지 못했습니다. 기존 백업을 보존하고 무결성을 확인하세요.')
    stream.seek(begin)
    data=stream.read(min(65536,end-begin))
    for separator in (b'\r\n\r\n',b'\n\n'):
        if separator in data:
            data=data.split(separator,1)[0]
            break
    data=data.rstrip(b'\r\n')
    fields=[name_hex,begin,hashlib.sha256(data).hexdigest()]
    return hashlib.sha256(json.dumps(fields,separators=(',',':')).encode('ascii')).hexdigest()


def message_digest(stream,begin,end,progress=lambda done,total:None):
    """Ignore only trailing MBOX separator newlines, using bounded reads."""
    if type(begin) is not int or type(end) is not int or not 0<=begin<=end<=os.fstat(stream.fileno()).st_size:
        raise CollectionError('메일 원본의 저장 위치·크기를 확인하지 못했습니다.')
    stream.seek(begin)
    digest=hashlib.sha256();stable=digest.copy();done=0
    while done<end-begin:
        block=stream.read(min(1024*1024,end-begin-done))
        if not block:raise CollectionError('메일 원본이 중간에 끝났습니다.')
        before=digest.copy();digest.update(block)
        trimmed=block.rstrip(b'\r\n')
        if len(trimmed)==len(block):stable=digest.copy()
        elif trimmed:
            stable=before;stable.update(trimmed)
        done+=len(block)
        progress(done,end-begin)
    return stable.hexdigest()


def load_tombstones(root, db, files):
    db.execute('CREATE TEMP TABLE archive_deleted(kind TEXT,key TEXT,content TEXT,PRIMARY KEY(kind,key,content))')
    path=root/TOMBSTONES
    if not path.exists():
        if TOMBSTONES in files:raise CollectionError('로컬 삭제 기록이 없습니다. 기존 백업을 보존하고 무결성을 확인하세요.')
        return 0
    if path.is_symlink() or digest_file(path)!=files.get(TOMBSTONES):
        raise CollectionError('로컬 삭제 기록의 검증 정보가 일치하지 않습니다.')
    batch=[]
    count=0
    with path.open('rb') as stream:
        while line:=stream.readline(65537):
            if len(line)>65536:raise CollectionError('로컬 삭제 기록을 확인하지 못했습니다.')
            value=json.loads(line)
            if value.get('protocol')!=PROTOCOL or value.get('kind') not in ('approval','mail') or not re.fullmatch('[a-f0-9]{64}',str(value.get('key',''))):
                raise CollectionError('로컬 삭제 기록을 확인하지 못했습니다.')
            content=value.get('content','')
            if value['kind']=='mail' and not re.fullmatch('[a-f0-9]{64}',str(content)):raise CollectionError('메일 삭제 기록을 확인하지 못했습니다.')
            batch.append((value['kind'],value['key'],content))
            count+=1
            if len(batch)==BATCH:
                db.executemany('INSERT OR IGNORE INTO archive_deleted VALUES (?,?,?)',batch)
                batch.clear()
    db.executemany('INSERT OR IGNORE INTO archive_deleted VALUES (?,?,?)',batch)
    return count


def removed(db,kind,key):
    return db.execute('SELECT 1 FROM archive_deleted WHERE kind=? AND key=?',(kind,key)).fetchone() is not None


def mail_removed(db,stream,source,begin,end):
    key=mail_key(stream,source,begin,end)
    if not removed(db,'mail',key):return False
    content=message_digest(stream,begin,end)
    return db.execute("SELECT 1 FROM archive_deleted WHERE kind='mail' AND key=? AND content=?",(key,content)).fetchone() is not None


def catalog_items(path):
    """Read the generated {items:[...]} catalog one document at a time."""
    decoder=json.JSONDecoder()
    with path.open('r',encoding='utf-8') as stream:
        buffer=''
        def more():
            nonlocal buffer
            block=stream.read(65536)
            buffer+=block
            return bool(block)
        def trim():
            nonlocal buffer
            while not buffer.strip():
                buffer=''
                if not more():raise CollectionError('결재 목록 JSON이 중간에 끝났습니다.')
            buffer=buffer.lstrip()
        trim()
        while not re.match(r'^\{\s*"items"\s*:\s*\[',buffer):
            if len(buffer)>4096 or not more():raise CollectionError('결재 목록 형식이 다릅니다.')
        buffer=buffer[re.match(r'^\{\s*"items"\s*:\s*\[',buffer).end():]
        first=True
        while True:
            trim()
            if buffer.startswith(']'):
                buffer=buffer[1:]
                tail=buffer+stream.read()
                if tail.strip()!='}':raise CollectionError('결재 목록 끝을 확인하지 못했습니다.')
                return
            if not first:
                if not buffer.startswith(','):raise CollectionError('결재 목록 구분자가 올바르지 않습니다.')
                buffer=buffer[1:]
                trim()
            first=False
            while True:
                try:
                    item,position=decoder.raw_decode(buffer)
                    break
                except json.JSONDecodeError:
                    if not more():raise CollectionError('결재 목록 JSON을 읽지 못했습니다.') from None
            if not isinstance(item,dict) or not isinstance(item.get('id'),str):raise CollectionError('결재 목록의 ID가 올바르지 않습니다.')
            yield item
            buffer=buffer[position:]


def owned_folder(identity,kind,html):
    if kind=='mail' and re.fullmatch(r'mail-[0-9]{9}',identity):
        return 'mail/offline/'+identity+'/'
    if kind=='approval' and identity.startswith('approval-'):
        document=identity[len('approval-'):]
        if re.fullmatch(r'[A-Za-z0-9_-]+',document):
            prefix='documents/'+document+'/'
            if isinstance(html,str) and html.startswith(prefix):return prefix
    return None


def file_folder(name):
    parts=name.split('/')
    if len(parts)>3 and parts[:2]==['mail','offline']:return '/'.join(parts[:3])+'/'
    if len(parts)>2 and parts[0]=='documents':return '/'.join(parts[:2])+'/'
    return ''


class DeletePlan:
    def __init__(self,root):
        self.root=Path(root).resolve()
        self.temporary=tempfile.TemporaryDirectory(prefix='segio-delete-selection-')
        self.path=Path(self.temporary.name)/'selection.sqlite3'
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE requested(id TEXT PRIMARY KEY)')
            db.execute('CREATE TABLE selected(id TEXT PRIMARY KEY,kind TEXT,html TEXT,source TEXT,begin INTEGER,end INTEGER)')
            db.execute('CREATE TABLE cleanup(prefix TEXT PRIMARY KEY,shared INTEGER DEFAULT 0)')
            db.execute('CREATE TABLE raw_cleanup(source TEXT PRIMARY KEY)')
        self.sealed=False
        self.count=0

    def add(self,ids):
        if self.sealed or not isinstance(ids,list) or len(ids)>BATCH:raise ValueError('한 번에 500개씩 선택을 전달하세요.')
        for identity in ids:
            if not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',identity):raise ValueError('선택 ID가 올바르지 않습니다.')
        with sqlite3.connect(self.path) as db:
            db.executemany('INSERT OR IGNORE INTO requested VALUES (?)',((value,) for value in ids))

    def seal(self,all_matches=False,filters=None):
        if self.sealed:raise ValueError('이미 확인한 선택입니다.')
        if type(all_matches) is not bool:raise ValueError('선택 범위를 확인하세요.')
        manifest=json.loads((self.root/'manifest.json').read_bytes())
        self.baseline=DateBaseline(self.root,manifest['user'])
        self.signature=self.baseline.original_manifest_signature
        filters=filters or {}
        if any(not isinstance(value,str) for value in filters.values()):raise ValueError('검색 범위를 확인하세요.')
        with closing(sqlite3.connect((self.root/'catalog.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
            db.execute('PRAGMA temp_store=FILE')
            db.execute('PRAGMA cache_size=-8192')
            db.execute('ATTACH DATABASE ? AS selection',(str(self.path),))
            origin,parameters,meta,label=search_filter(self.root,db,filters.get('q',''),filters.get('kind',''),filters.get('mailbox',''),filters.get('category',''))
            predicate='NOT EXISTS' if all_matches else 'EXISTS'
            origin+=' AND '+predicate+' (SELECT 1 FROM selection.requested r WHERE r.id=i.id)'
            has_mail=db.execute("SELECT 1 FROM sqlite_master WHERE name='mail_records'").fetchone()
            join=' LEFT JOIN mail_records deletion_mail ON deletion_mail.id=i.id ' if has_mail else ''
            fields='deletion_mail.source,deletion_mail.begin,deletion_mail.end' if has_mail else "'',0,0"
            # Join before WHERE; never expand a large selection into SQL IN parameters.
            origin=origin.replace(' WHERE ',join+' WHERE ',1)
            db.execute('INSERT INTO selection.selected SELECT i.id,i.kind,i.html,'+fields+origin,parameters)
            db.execute('CREATE INDEX selection.selected_source_index ON selected(source)')
            db.commit()
            if has_mail and db.execute("SELECT 1 FROM selection.selected WHERE kind='mail' LIMIT 1").fetchone():
                for source in self.baseline.sources:
                    if source.get('format')!='rfc822':continue
                    name=source['path']
                    if (db.execute('SELECT 1 FROM selection.selected WHERE source=? LIMIT 1',(name,)).fetchone() and
                            not db.execute('SELECT 1 FROM mail_records m WHERE source=? AND NOT EXISTS (SELECT 1 FROM selection.selected s WHERE s.id=m.id) LIMIT 1',(name,)).fetchone()):
                        db.execute('INSERT INTO selection.raw_cleanup VALUES (?)',(name,))
            for identity,kind,html in db.execute('SELECT id,kind,html FROM selection.selected'):
                prefix=owned_folder(identity,kind,html)
                if prefix:db.execute('INSERT OR IGNORE INTO selection.cleanup(prefix) VALUES (?)',(prefix,))
            for identity,kind,html in db.execute('SELECT i.id,i.kind,i.html FROM items i WHERE NOT EXISTS (SELECT 1 FROM selection.selected d WHERE d.id=i.id)'):
                prefix=file_folder(html or '')
                if prefix:db.execute('UPDATE selection.cleanup SET shared=1 WHERE prefix=?',(prefix,))
            db.commit()
            self.count=db.execute('SELECT count(*) FROM selection.selected').fetchone()[0]
        if not self.count:raise ValueError('삭제할 항목을 선택하세요.')
        self.sealed=True
        return self.count

    def close(self):
        self.temporary.cleanup()
        self.baseline=None

    def run(self,progress=lambda *args:None,clean_files=True):
        if not self.sealed:raise ValueError('먼저 선택을 확인하세요.')
        baseline=self.baseline
        staging=None
        warnings=[]
        def log(message):
            if '정리를 확인하지 못했습니다' in message:warnings.append('이전 백업 사본의 파일 정리를 확인하지 못했습니다.')
        with update_lock(self.root):
            if digest_file(self.root/'manifest.json')!=self.signature:raise CollectionError('선택 확인 후 백업이 변경되었습니다. 목록을 새로 열어 확인하세요.')
            staging=Path(tempfile.mkdtemp(prefix='.segio-delete-',dir=self.root.parent))
            try:
                manifest=json.loads((self.root/'manifest.json').read_bytes())
                files={}
                with closing(sqlite3.connect(self.path)) as plan:
                    plan.execute('PRAGMA temp_store=FILE')
                    plan.execute('PRAGMA cache_size=-8192')
                    if clean_files and plan.execute('SELECT 1 FROM cleanup WHERE shared=1 LIMIT 1').fetchone():
                        warnings.append('다른 항목이 함께 사용하는 문서 파일은 보존했습니다.')
                    total=len(manifest['files'])
                    for number,(name,expected) in enumerate(manifest['files'].items(),1):
                        if name=='index.html' or name.startswith('search/'):continue
                        if clean_files and plan.execute('SELECT 1 FROM raw_cleanup WHERE source=?',(name,)).fetchone():continue
                        prefix=file_folder(name)
                        if clean_files and prefix and plan.execute('SELECT 1 FROM cleanup WHERE prefix=? AND shared=0',(prefix,)).fetchone():continue
                        source=baseline.raw_path(name)
                        target=staging/name
                        target.parent.mkdir(parents=True,exist_ok=True)
                        if name in ('catalog.sqlite3','catalog.json','mail/sources.json',TOMBSTONES):
                            baseline.copy(source,target,expected,lambda:None,lambda *a,**k:None,relative=name)
                        else:
                            try:os.link(source,target)
                            except FileNotFoundError:
                                from .attachment_safety import attachment_name
                                if attachment_name(name):continue
                                raise
                            except OSError as exc:
                                if getattr(exc,'winerror',None) in (225,226) or exc.errno not in (errno.EXDEV,errno.EPERM,errno.EACCES,errno.ENOTSUP):raise
                                baseline.copy(source,target,expected,lambda:None,lambda *a,**k:None,relative=name)
                        files[name]=expected
                        if number%200==0:progress('파일 준비',number,total)
                    progress('목록 삭제',0,self.count)
                    with (staging/TOMBSTONES).open('ab') as output:
                        sources=json.loads((self.root/'mail/sources.json').read_bytes()).get('sources',[]) if (self.root/'mail/sources.json').is_file() else []
                        names={item['path']:item['name_bytes_hex'] for item in sources}
                        from .portable_mail import SourceReader
                        with SourceReader() as opened:
                            for number,(identity,kind,source,begin,end) in enumerate(plan.execute('SELECT id,kind,source,begin,end FROM selected ORDER BY kind,source,begin'),1):
                                record={'protocol':PROTOCOL,'kind':kind}
                                if kind=='approval':key=approval_key(identity)
                                elif kind=='mail':
                                    stream=opened.open(baseline.raw_path(source))
                                    key=mail_key(stream,names[source],begin,end)
                                    record['content']=message_digest(stream,begin,end,lambda done,total:progress('메일 원문 확인 ('+str(number)+'/'+str(self.count)+')',done,total))
                                else:raise CollectionError('삭제할 자료 종류가 올바르지 않습니다.')
                                record['key']=key
                                output.write((json.dumps(record,separators=(',',':'))+'\n').encode('ascii'))
                                if number%BATCH==0:progress('목록 삭제',number,self.count)
                    catalog=staging/'catalog.json'
                    temporary=catalog.with_suffix('.pending')
                    with temporary.open('w',encoding='utf-8') as output:
                        output.write('{"items":[')
                        first=True
                        for item in catalog_items(catalog):
                            if plan.execute('SELECT 1 FROM selected WHERE id=?',('approval-'+item['id'],)).fetchone():continue
                            if not first:output.write(',')
                            first=False
                            output.write(json.dumps(item,ensure_ascii=False,separators=(',',':')))
                        output.write(']}')
                    os.replace(temporary,catalog)
                    if clean_files and (staging/'mail/sources.json').is_file():
                        sources=json.loads((staging/'mail/sources.json').read_bytes())
                        sources['sources']=[item for item in sources.get('sources',[]) if not plan.execute('SELECT 1 FROM raw_cleanup WHERE source=?',(item['path'],)).fetchone()]
                        atomic_write(staging/'mail/sources.json',json.dumps(sources,ensure_ascii=False).encode('utf-8'))
                        manifest['counts']['mail_files']=len(sources['sources'])
                with closing(sqlite3.connect(staging/'catalog.sqlite3')) as db:
                    db.execute('PRAGMA temp_store=FILE')
                    db.execute('PRAGMA cache_size=-8192')
                    db.execute('ATTACH DATABASE ? AS deletion_plan',(str(self.path),))
                    db.execute('PRAGMA secure_delete=ON')
                    tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    for table in ('mail_dates','document_records','mail_records','items'):
                        if table in tables:db.execute('DELETE FROM '+table+' WHERE id IN (SELECT id FROM deletion_plan.selected)')
                    db.commit()
                    for kind,count in db.execute('SELECT kind,count(*) FROM items GROUP BY kind'):
                        manifest['counts'][kind]=count
                    for kind in ('approval','mail'):
                        if not db.execute('SELECT 1 FROM items WHERE kind=? LIMIT 1',(kind,)).fetchone():manifest['counts'][kind]=0
                manifest['files']=files
                for entry in manifest.get('coverage',[]):
                    if entry.get('kind')=='mail_index':entry['saved']=manifest['counts'].get('mail',0)
                manifest.pop('browser_index',None)
                manifest['local_edits']={'protocol':PROTOCOL,'deleted_count':self.count,'edited_at':int(time.time())}
                for name in ('catalog.json','catalog.sqlite3',TOMBSTONES):files[name]=digest_file(staging/name)
                if (staging/'mail/sources.json').is_file():files['mail/sources.json']=digest_file(staging/'mail/sources.json')
                atomic_write(staging/'manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8'))
                from .portable_mail import build_portable_mail
                progress('검색 목록 갱신',0,None)
                manifest=build_portable_mail(staging,manifest,changed_sources=set(),audit=True,
                    progress=lambda stage,done=None,total=None,**kw:progress('메일 상태 확인',done,total))
                from .search_page import install_search_page
                install_search_page(staging,manifest,progress=lambda stage,done=None,total=None,**kw:progress('검색 목록 갱신',done,total))
                progress('삭제 반영',self.count,self.count)
                baseline._commit(staging,lambda:None,log)
                staging=None
                return {'deleted':self.count,'warnings':warnings,'shared_mail_originals_retained':True}
            finally:
                if staging and staging.is_dir():
                    try:shutil.rmtree(staging)
                    except OSError:pass
