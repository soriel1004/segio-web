"""Reuse verified, unchanged raw mail in a new independent personal snapshot."""
import hashlib
import json
import re
from pathlib import Path

from .archive import atomic_write
from .backup_folders import (require_backup_file, BaselineValidationError,
                             read_backup_metadata, validate_baseline_manifest)
from .file_ops import merge_io
from .compact_mail import digest_file
from .storage import require_space
from .web import CollectionError

PROTOCOL='segio-incremental/1'


class Baseline:
    def __init__(self, root, user, checkpoint=lambda:None):
        self.root=Path(root).expanduser().resolve()
        self.entries={}
        checkpoint()
        path=require_backup_file(self.root,'manifest.json')
        with merge_io('read_previous','manifest.json'):
            manifest,_=read_backup_metadata(path,checkpoint)
        validate_baseline_manifest(manifest,user)
        self.load_mail_sources(manifest,user,checkpoint)

    def load_mail_sources(self, manifest, user, checkpoint):
        """Validate sources against the already-read manifest in date-based updates."""
        reason='manifest_invalid'
        try:
            self.finished_at=manifest.get('finished_at','')
            reason='mail_list_changed'
            source_path=self.root/'mail/sources.json'
            require_backup_file(self.root,'mail/sources.json')
            expected=manifest['files'].get('mail/sources.json')
            if not expected:
                raise ValueError()
            with merge_io('read_previous','mail/sources.json'):
                sources,actual=read_backup_metadata(source_path,checkpoint,kind='mail_sources')
                if actual!=expected:
                    raise BaselineValidationError('mail_list_changed')
            if sources.get('user')!=user or sources.get('raw_files_complete') is False:
                raise ValueError()
            if not isinstance(sources.get('sources'),list):
                raise ValueError()
            self.sources=sources['sources']
            for item in sources['sources']:
                checkpoint()
                name=item['name_bytes_hex']
                relative=item['path']
                info={key:item[key] for key in ('bytes','sha256')}
                if (not re.fullmatch(r'(?:[a-f0-9]{2}){1,4096}',name) or name in self.entries or
                        not re.fullmatch(r'mail/raw/[0-9]{6}\.(bin|mbox|eml)',relative) or
                        type(info['bytes']) is not int or info['bytes']<0 or
                        not re.fullmatch(r'[a-f0-9]{64}',info['sha256']) or manifest['files'].get(relative)!=info):
                    raise ValueError()
                path=self.raw_path(relative)
                require_backup_file(self.root,relative)
                with merge_io('inspect_previous',relative):
                    if path.stat().st_size!=info['bytes']:
                        raise BaselineValidationError('mail_size_changed')
                self.entries[name]=dict(info,path=relative)
        except (ValueError,KeyError,TypeError,AttributeError):
            raise BaselineValidationError(reason) from None

    def raw_path(self, relative):
        path=self.root/relative
        if not path.resolve().is_relative_to(self.root) or any(part.is_symlink() for part in (path,*path.parents) if part!=self.root):
            raise BaselineValidationError('unsafe_path')
        return path

    def payload(self):
        entries=[dict(name_bytes_hex=name,bytes=info['bytes'],sha256=info['sha256'])
                 for name,info in self.entries.items()]
        value=json.dumps({'protocol':PROTOCOL,'files':entries},separators=(',',':'))
        if len(entries)>1000 or len(value.encode())>48000:
            raise CollectionError('이전 백업의 메일 원본 목록이 변경분 비교 한도를 넘었습니다. 이번에는 전체 백업을 선택하세요.')
        return value

    def restore(self, destination, checkpoint, report):
        manifest_path=destination/'manifest.json'
        manifest=json.loads(manifest_path.read_bytes())
        delta=manifest.get('incremental')
        if not isinstance(delta,dict) or delta.get('protocol')!=PROTOCOL:
            raise CollectionError('서버가 변경분 백업 검증 정보를 반환하지 않았습니다. 서버 tools를 갱신하세요.')
        reused=delta.get('reused_mail')
        if not isinstance(reused,dict) or len(reused)>len(self.entries):
            raise CollectionError('서버의 메일 재사용 목록이 올바르지 않습니다.')
        sources=json.loads((destination/'mail/sources.json').read_bytes())['sources']
        by_path={item['path']:item for item in sources}
        total=sum(item['bytes'] for item in reused.values())
        require_space(destination,total+64*1024*1024,'이전 메일 원본 복사')
        done=0
        report('reuse',0,total)
        for relative,item in reused.items():
            checkpoint()
            name=item.get('name_bytes_hex')
            old=self.entries.get(name)
            expected={key:item.get(key) for key in ('bytes','sha256')}
            source=by_path.get(relative,{})
            if (not re.fullmatch(r'mail/raw/[0-9]{6}\.bin',relative) or relative in manifest['files'] or not old or
                    expected!={key:old[key] for key in expected} or source.get('name_bytes_hex')!=name or
                    any(source.get(key)!=value for key,value in expected.items())):
                raise CollectionError('서버 재사용 목록과 이전 백업의 메일 검증 정보가 일치하지 않습니다.')
            target=destination/relative
            target.parent.mkdir(parents=True,exist_ok=True)
            digest,count=hashlib.sha256(),0
            with self.raw_path(old['path']).open('rb') as incoming,target.open('xb') as outgoing:
                while block:=incoming.read(1024*1024):
                    checkpoint()
                    count+=len(block)
                    if count>expected['bytes']:
                        raise CollectionError('이전 백업의 메일 원본 크기가 변경되었습니다.')
                    outgoing.write(block)
                    digest.update(block)
                    report('reuse',done+count,total)
            if count!=expected['bytes'] or digest.hexdigest()!=expected['sha256']:
                raise CollectionError('이전 백업의 메일 원본 SHA-256이 변경되었습니다. 이전 백업을 확인하거나 전체 백업을 실행하세요.')
            manifest['files'][relative]=expected
            done+=count
        delta['reused_files']=len(reused)
        delta['reused_bytes']=done
        sources_path=destination/'mail/sources.json'
        source_record=json.loads(sources_path.read_bytes())
        source_record['raw_files_complete']=True
        atomic_write(sources_path,json.dumps(source_record,ensure_ascii=False,indent=2).encode())
        manifest['files']['mail/sources.json']=digest_file(sources_path,checkpoint)
        # Each result has its own raw files, index and attachments. No links or
        # writes into the chosen baseline, which can be removed independently.
        atomic_write(manifest_path,json.dumps(manifest,ensure_ascii=False,indent=2).encode())
        report('reuse',total,total)
        return len(reused),done
