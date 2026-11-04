"""Small loopback API for streamed selections and confirmed local deletion jobs."""
import secrets
import threading
import time
import json

from .local_delete import DeletePlan


class DeleteService:
    def __init__(self,root):
        self.root=root
        self.lock=threading.RLock()
        self.jobs={}
        self.initial=None

    def create(self,fields):
        with self.lock:
            if any(item['state']=='running' for item in self.jobs.values()):raise ValueError('삭제 작업이 진행 중입니다.')
            for identifier,item in list(self.jobs.items()):
                if time.monotonic()-item['created']>900 or len(self.jobs)>=8:
                    item['plan'].close()
                    del self.jobs[identifier]
            if type(fields.get('all',False)) is not bool:raise ValueError('선택 범위가 올바르지 않습니다.')
            filters=fields.get('filters',{})
            manifest=json.loads((self.root/'manifest.json').read_bytes())
            revision=fields.get('revision')
            if not isinstance(revision,str) or revision!=manifest.get('browser_edit_revision'):
                raise ValueError('백업 목록이 변경되었습니다. 인덱스를 새로 열고 선택하세요.')
            if not isinstance(filters,dict) or set(filters)-{'q','kind','mailbox','category'}:raise ValueError('검색 범위가 올바르지 않습니다.')
            if any(not isinstance(value,str) or len(value)>4096 for value in filters.values()):raise ValueError('검색 범위가 올바르지 않습니다.')
            from .index_metadata import CATEGORIES
            if filters.get('kind','') not in ('','approval','mail') or filters.get('category','') not in ('',*CATEGORIES):raise ValueError('검색 범위가 올바르지 않습니다.')
            identifier=secrets.token_hex(16)
            self.jobs[identifier]={'plan':DeletePlan(self.root),'all':fields.get('all',False),
                                   'revision':revision,
                                   'filters':filters,'state':'selecting','phase':'선택 확인','completed':0,'total':None,
                                   'created':time.monotonic()}
            return {'selection':identifier}

    def get(self,identifier):
        if not isinstance(identifier,str) or identifier not in self.jobs:raise ValueError('삭제 작업을 찾지 못했습니다.')
        return self.jobs[identifier]

    def add(self,identifier,ids):
        with self.lock:
            item=self.get(identifier)
            if item['state']!='selecting':raise ValueError('선택 전달이 끝났습니다.')
            item['plan'].add(ids)
            return {'accepted':len(ids)}

    def preview(self,identifier):
        with self.lock:
            item=self.get(identifier)
            if item['state']=='preview':return {'count':item['plan'].count}
            if item['state']!='selecting':raise ValueError('선택 상태가 변경되었습니다.')
            count=item['plan'].seal(item['all'],item['filters'])
            if item['plan'].baseline.manifest.get('browser_edit_revision')!=item['revision']:
                item['plan'].close()
                item['state']='cancelled'
                raise ValueError('선택을 전달하는 동안 백업이 변경되었습니다. 인덱스를 새로 열고 선택하세요.')
            item['state']='preview'
            item['total']=count
            return {'count':count}

    def start(self,identifier,clean_files=True):
        with self.lock:
            item=self.get(identifier)
            if item['state']!='preview' or type(clean_files) is not bool:raise ValueError('삭제할 건수를 먼저 확인하세요.')
            if any(job['state']=='running' for job in self.jobs.values()):raise ValueError('삭제 작업이 진행 중입니다.')
            item['state']='running'
            def progress(phase,completed,total):
                with self.lock:item.update(phase=phase,completed=completed,total=total)
            def run():
                try:
                    result=item['plan'].run(progress,clean_files)
                    with self.lock:item.update(state='done',result=result,phase='삭제 완료',completed=item['plan'].count,total=item['plan'].count)
                except Exception as exc:
                    from .web import CollectionError
                    message=(str(exc) if isinstance(exc,(CollectionError,ValueError)) else
                             '파일 접근 또는 저장 공간 문제로 삭제를 완료하지 못했습니다. 기존 백업은 유지됩니다. 파일·첨부를 닫고 폴더 접근 권한을 확인하세요.')
                    from .db_client import processing_error
                    from .client_diagnostics import failure_notice
                    from .preferences import load_output_folder
                    from pathlib import Path
                    error=exc if isinstance(exc,CollectionError) else processing_error(exc,'PC 백업 삭제')
                    message+=failure_notice(Path(load_output_folder(self.root.parent)),error,'PC 백업 삭제')
                    with self.lock:item.update(state='failed',message=message,phase='삭제 중단')
                finally:item['plan'].close()
            threading.Thread(target=run,name='segio-local-delete',daemon=False).start()
            return {'started':True}

    def status(self,identifier):
        with self.lock:
            item=self.get(identifier)
            return {key:item[key] for key in ('state','phase','completed','total','message','result') if key in item}

    def cancel(self,identifier):
        with self.lock:
            item=self.get(identifier)
            if item['state'] not in ('selecting','preview'):raise ValueError('이미 처리 중인 작업입니다.')
            item['plan'].close()
            item['state']='cancelled'
            return {'cancelled':True}

    def close(self):
        with self.lock:
            for item in self.jobs.values():
                if item['state'] in ('selecting','preview'):
                    item['plan'].close()
                    item['state']='cancelled'
