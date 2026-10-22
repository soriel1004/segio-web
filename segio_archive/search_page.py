"""Portable archive list/search, including file:// without fetching SQLite."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from .archive import atomic_write
from .mail_folders import MailFolders
from .index_metadata import IndexMetadata, CATEGORIES, item_date

VERSION = 'offline-search/7'
CHUNK_SIZE = 500


def script_json(value):
    # Metadata is data, even when a title contains HTML or script delimiters.
    return json.dumps(value, ensure_ascii=True, separators=(',', ':')).replace('<', '\\u003c')


def install_search_page(root: Path, manifest=None, checkpoint=lambda: None, progress=None):
    """Update only presentation/search metadata; never read or rewrite raw MBOX."""
    root = root.resolve()
    existing = manifest is None
    if existing:
        manifest = json.loads((root/'manifest.json').read_text(encoding='utf-8'))
        if (manifest.get('browser_index') == VERSION and manifest.get('browser_status_map',{})==manifest.get('approval_status_map',{}) and
                all((root/name).is_file() for name in manifest['files'] if name.startswith('search/'))):
            return manifest
        expected = manifest['files'].get('index.html')
        if expected and hashlib.sha256((root/'index.html').read_bytes()).hexdigest() != expected['sha256']:
            raise ValueError('index.html이 변경되어 화면을 자동 갱신하지 않았습니다. 파일 무결성을 확인하세요.')
    files, counts, chunks = manifest['files'], {'approval': 0, 'mail': 0}, []
    from .document_page import upgrade_documents
    upgrade_documents(root, manifest, checkpoint)

    def write(name, value):
        if not (root/name).resolve().is_relative_to(root):
            raise ValueError('검색 화면 파일이 보관 폴더를 벗어납니다.')
        payload = value.encode('utf-8')
        atomic_write(root/name, payload)
        files[name] = {'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}

    folders=MailFolders(root)
    report=progress or (lambda *args,**kwargs:None)
    approval_counts={key:0 for key in CATEGORIES}
    with closing(sqlite3.connect((root/'catalog.sqlite3').as_uri()+'?mode=ro', uri=True)) as db:
        boxes=folders.boxes(db)
        meta=IndexMetadata(root,db,manifest)
        for kind, count in db.execute('SELECT kind,count(*) FROM items GROUP BY kind'):
            if kind in counts:
                counts[kind] = count
        done,total=0,sum(counts.values())
        report('search')
        cursor = db.execute('SELECT i.id,i.kind,i.title,i.folders,i.html,i.body,'+meta.date_sql+','+meta.payload_sql+' FROM items i '+meta.joins+' ORDER BY '+meta.order)
        report('search',0,total,unit='messages')
        with closing(cursor):
            while rows := cursor.fetchmany(CHUNK_SIZE):
                checkpoint()
                name = f'search/part-{len(chunks):06d}.js'
                packed=[]
                for identity,kind,title,folder,link,body,date_raw,payload in rows:
                    categories=meta.categories(payload) if kind=='approval' else []
                    for key in categories:
                        approval_counts[key]+=1
                    info=folders.info(folder) if kind=='mail' else None
                    if kind=='mail' and manifest.get('portable_mail'):
                        from .portable_mail import page_path
                        link=page_path(identity)
                    packed.append((identity,kind,title,info['label'] if info else ' · '.join(CATEGORIES[key] for key in categories),
                                   link,body,info['id'] if info else '',folder,
                                   item_date(kind,date_raw,payload),date_raw if kind=='mail' else '',categories))
                rows=packed
                write(name, 'window.archiveChunk='+script_json(rows)+';')
                chunks.append(name)
                done+=len(rows)
                report('search',done,total,unit='messages')
    warning_count=manifest.get('portable_mail',{}).get('warning_count',0)
    config = {'counts': counts, 'chunks': chunks, 'mailboxes': boxes,
              'mail_warning_count':warning_count if type(warning_count) is int and warning_count>=0 else 0,
              'approval_categories':[{'id':key,'label':label,'count':approval_counts[key]} for key,label in CATEGORIES.items()]}
    write('index.html', INDEX.replace('__ARCHIVE_CONFIG__', script_json(config)))
    if manifest.get('format')=='segio-db-html/1' and manifest.get('viewer')!='sqlite-mail/1':
        manifest['viewer']='local-db/1'
    manifest['browser_index'] = VERSION
    manifest['browser_status_map']=manifest.get('approval_status_map',{})
    atomic_write(root/'manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'))
    return manifest


INDEX = r'''<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'self' file: 'unsafe-inline'; connect-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>그룹웨어 문서 및 메일 백업 및 검색</title>
<style>
body{font:12px 'Malgun Gothic',sans-serif;line-height:1.6;background:#f3f5f8;color:#162234;max-width:1100px;margin:24px auto;padding:0 18px}
h1{font-size:20px}h2{font-size:14px;margin:0}input,button,select{font:inherit;box-sizing:border-box}button{cursor:pointer;padding:8px 18px;border:1px solid #a4b1c2;background:white;border-radius:5px}button:disabled{cursor:default;opacity:.45}button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid #075b9c;outline-offset:2px}
#search-form{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0}#q{flex:1;min-width:160px;padding:10px;border:1px solid #a4b1c2;border-radius:5px}#scope{padding:8px;border:1px solid #a4b1c2;border-radius:5px;background:white}#search-button,[role=tab][aria-selected=true]{background:#075b9c;color:white;border-color:#075b9c}
[role=tablist]{display:flex;gap:8px;border-bottom:2px solid #075b9c;padding-bottom:8px}#mail-folders,#approval-folders{background:white;border:1px solid #d8e0e9;border-radius:6px;padding:12px;margin:12px 0}.box-list{display:flex;flex-wrap:wrap;gap:6px;max-height:180px;overflow:auto}.box-list button{padding:6px 12px}.box-list button[aria-pressed=true]{background:#075b9c;color:white;border-color:#075b9c}#custom-mailboxes h2{font-size:12px;margin:12px 0 6px}#list-head{display:grid;grid-template-columns:42px minmax(0,1fr) minmax(100px,20%) minmax(150px,22%);gap:10px;padding:8px 12px;font-weight:bold}#summary{margin:12px 0}#results{background:white;border:1px solid #d8e0e9;border-radius:6px}article{display:grid;grid-template-columns:42px minmax(0,1fr) minmax(100px,20%) minmax(150px,22%);gap:10px;padding:12px;border-bottom:1px solid #e3e8ef;align-items:center}article:last-child{border-bottom:0}article:hover,article:focus-within{background:#eaf3ff}article h2 a{text-decoration:none;display:block}article h2 a:focus-visible{outline:2px solid #075b9c;outline-offset:3px}article h2{font-size:12px;font-weight:normal;overflow-wrap:anywhere}article p{margin:0;overflow-wrap:anywhere;color:#526176}a{color:#075b9c}#notice{color:#76551b}#pagination{display:flex;gap:14px;align-items:center;margin-top:16px}#empty{padding:20px}@media(max-width:600px){article{grid-template-columns:35px minmax(0,1fr)}article p{grid-column:2}}
</style>
<h1>그룹웨어 문서 및 메일 백업 및 검색</h1>
<p>메일 제목·보낸 사람·받는 사람과 결재 내용을 검색합니다. 한 페이지에 100건씩 표시합니다.</p>
<p id="notice" role="status"></p>
<form id="search-form" role="search"><select id="scope" aria-label="검색 범위"><option value="">전체</option><option value="approval">결재</option><option value="mail">메일</option></select><input id="q" aria-label="검색어" placeholder="제목, 보낸 사람, 받는 사람 또는 결재 내용"><button id="search-button" type="submit">검색</button></form>
<div role="tablist" aria-label="백업 자료 종류"><button id="tab-all" role="tab" aria-selected="false" aria-controls="results" type="button">전체</button><button id="tab-approval" role="tab" aria-selected="true" aria-controls="results" type="button">결재</button><button id="tab-mail" role="tab" aria-selected="false" aria-controls="results" type="button">메일</button></div>
<section id="mail-folders" hidden aria-label="메일함 분류"><div id="base-mailboxes" class="box-list" role="group" aria-label="기본 메일함"></div><div id="custom-mailboxes"><h2>편지 보관함</h2><div class="box-list" role="group" aria-label="편지 보관함"></div></div></section>
<section id="approval-folders" hidden aria-label="결재 문서 분류"><div class="box-list" role="group" aria-label="결재 문서 분류"></div><p>상태를 확인할 수 없는 문서는 상태 미확인에 표시합니다. 참조·수신 문서는 다른 상태 메뉴에도 함께 표시될 수 있습니다.</p></section>
<p id="summary" role="status"></p><div id="list-head"><span>번호</span><span id="title-label">제목</span><span id="folder-label">메일함</span><span id="date-label">메일 헤더 날짜</span></div><main id="results" role="tabpanel" aria-labelledby="tab-approval"></main>
<div id="pagination"><button id="prev" type="button">이전</button><span id="page"></span><button id="next" type="button">다음</button></div>
<script>
'use strict';
const config=__ARCHIVE_CONFIG__,offline=location.protocol==='file:',q=document.querySelector('#q');
const initial=location.hash.slice(1).split('&'),labels={approval:'결재',mail:'메일',all:'전체'};
let page=0,kind=initial[0]==='all'?'':initial[0]==='mail'?'mail':(config.counts.approval?'approval':'mail'),query='',mailbox=new URLSearchParams(initial.slice(1).join('&')).get('box')||'',category=new URLSearchParams(initial.slice(1).join('&')).get('category')||'',sequence=0,pending=Promise.resolve();
if(!config.mailboxes.some(box=>box.id===mailbox))mailbox='';
if(!config.approval_categories.some(item=>item.id===category))category='';
const $=selector=>document.querySelector(selector);
function locationHash(){location.hash=(kind||'all')+(kind==='mail'&&mailbox?'&'+new URLSearchParams({box:mailbox}):kind==='approval'&&category?'&'+new URLSearchParams({category}):'')}
function mailboxButton(parent,id,label,count){const button=document.createElement('button');button.type='button';button.dataset.mailbox=id;button.textContent=label+' ('+count.toLocaleString()+'건)';button.onclick=()=>{mailbox=id;page=0;locationHash();load()};parent.append(button)}
mailboxButton($('#base-mailboxes'),'','전체 메일',config.counts.mail);
for(const box of config.mailboxes){mailboxButton(box.category==='custom'?$('#custom-mailboxes .box-list'):$('#base-mailboxes'),box.id,box.label,box.count)}
$('#custom-mailboxes').hidden=!config.mailboxes.some(box=>box.category==='custom');
function approvalButton(id,label,count){const button=document.createElement('button');button.type='button';button.dataset.category=id;button.textContent=label+' ('+count.toLocaleString()+'건)';button.onclick=()=>{category=id;page=0;locationHash();load()};$('#approval-folders .box-list').append(button)}
approvalButton('','전체 결재',config.counts.approval);for(const item of config.approval_categories)approvalButton(item.id,item.label,item.count);
function tabs(){
$('#mail-folders').hidden=kind!=='mail';$('#approval-folders').hidden=kind!=='approval';
for(const button of document.querySelectorAll('[data-category]'))button.setAttribute('aria-pressed',String(button.dataset.category===category));
$('#title-label').textContent=kind==='mail'?'메일 제목':kind==='approval'?'결재 제목':'제목';
$('#folder-label').textContent=kind==='mail'?'메일함':'자료 구분';
$('#date-label').textContent=kind==='mail'?'메일 헤더 날짜':kind==='approval'?'기안일자':'기안일자 / 메일 헤더 날짜';
for(const button of document.querySelectorAll('[data-mailbox]'))button.setAttribute('aria-pressed',String(button.dataset.mailbox===mailbox));
for(const key of ['all','approval','mail']){const count=key==='all'?config.counts.approval+config.counts.mail:config.counts[key];$('#tab-'+key).textContent=labels[key]+' ('+count.toLocaleString()+'건)';$('#tab-'+key).setAttribute('aria-selected',String((key==='all'?'':key)===kind))}
$('#results').setAttribute('aria-labelledby','tab-'+(kind||'all'))}
function readChunk(name){return new Promise((resolve,reject)=>{const script=document.createElement('script');script.src=name;script.onload=()=>{const rows=window.archiveChunk;delete window.archiveChunk;script.remove();Array.isArray(rows)?resolve(rows):reject(Error('색인 형식'))};script.onerror=()=>{script.remove();reject(Error('색인 파일'))};document.head.append(script)})}
async function offlineSearch(seq){let total=0,items=[];const needle=query.toLowerCase();for(const name of config.chunks){if(seq!==sequence)return null;const rows=await readChunk(name);for(const row of rows){if((kind&&row[1]!==kind)||(kind==='mail'&&mailbox&&row[6]!==mailbox)||(kind==='approval'&&category&&!row[10].includes(category))||![row[2],row[3],row[5],row[7]].some(value=>String(value||'').toLowerCase().includes(needle)))continue;if(total>=page*100&&items.length<100)items.push({id:row[0],kind:row[1],title:row[2],folder:row[3],html:row[4],detail:row[5],folder_id:row[6],source_folder:row[7],date:row[8],date_raw:row[9],categories:row[10]});total++}if(!needle&&items.length===100){total=kind==='mail'&&mailbox?config.mailboxes.find(box=>box.id===mailbox).count:kind==='approval'&&category?config.approval_categories.find(item=>item.id===category).count:kind?config.counts[kind]:config.counts.approval+config.counts.mail;break}}return {total,items}}
function render(data){$('#summary').textContent=labels[kind||'all']+' '+data.total.toLocaleString()+'건'+(kind==='mail'&&mailbox?' · '+config.mailboxes.find(box=>box.id===mailbox).label:'')+(kind==='approval'&&category?' · '+config.approval_categories.find(item=>item.id===category).label:'')+(query?' · 검색어: '+query:' · 전체 백업 목록');$('#page').textContent=(page+1)+' / '+Math.max(1,Math.ceil(data.total/100));$('#prev').disabled=page===0;$('#next').disabled=(page+1)*100>=data.total;const out=$('#results');out.replaceChildren();if(!data.items.length){const empty=document.createElement('p');empty.id='empty';empty.textContent=query?'검색 결과가 없습니다.':'이 종류의 백업 자료가 없습니다.';out.append(empty)}for(const [index,item] of data.items.entries()){const row=document.createElement('article'),number=document.createElement('span'),heading=document.createElement('h2'),link=document.createElement('a'),folder=document.createElement('p'),date=document.createElement('time');number.textContent=String(page*100+index+1);link.textContent=item.title||'제목 없음';link.href=String(item.html||'').split('/').map(encodeURIComponent).join('/');heading.append(link);folder.textContent=item.folder;folder.className='mailbox-name';folder.title=item.source_folder||item.folder;date.className='message-date';date.textContent=item.date||'';date.title=item.date_raw||item.date||'';row.append(number,heading,folder,date);out.append(row)}}
function load(){const seq=++sequence;tabs();$('#results').replaceChildren();$('#summary').textContent='목록을 불러오는 중…';$('#prev').disabled=$('#next').disabled=true;pending=pending.catch(()=>{}).then(async()=>{if(seq!==sequence)return;try{let data;if(offline){data=await offlineSearch(seq)}else{const response=await fetch('api/search?'+new URLSearchParams({q:query,kind,page,mailbox:kind==='mail'?mailbox:'',category:kind==='approval'?category:''}));if(!response.ok)throw Error('검색 응답');data=await response.json()}if(seq!==sequence||!data)return;render(data)}catch(error){if(seq!==sequence)return;$('#summary').textContent='목록을 불러오지 못했습니다.';$('#results').replaceChildren();$('#notice').textContent='백업 폴더의 search 폴더를 함께 보관하세요. Windows 프로그램의 보관함 열기로 열면 검색 화면을 갱신할 수 있습니다.'}})}
function chooseScope(value){kind=value;mailbox=category='';page=0;locationHash();load()}
$('#search-form').onsubmit=event=>{event.preventDefault();query=q.value.trim().slice(0,200);if(kind!==$('#scope').value){mailbox=category=''}kind=$('#scope').value;page=0;locationHash();load()};
$('#scope').onchange=()=>chooseScope($('#scope').value);
for(const key of ['all','approval','mail']){$('#tab-'+key).onclick=()=>{$('#scope').value=key==='all'?'':key;chooseScope($('#scope').value)}}
$('#prev').onclick=()=>{if(page>0){page--;load()}};$('#next').onclick=()=>{page++;load()};$('#notice').textContent='백업 폴더에서 결재·메일 본문·첨부를 바로 열 수 있습니다. 폴더 전체를 함께 보관하세요.';if(config.mail_warning_count){$('#notice').append(document.createTextNode(' 메일 '+config.mail_warning_count+'건은 변환·첨부 확인이 필요합니다. '));const report=document.createElement('a');report.href='mail/processing-issues.html';report.textContent='확인 필요 메일 목록';$('#notice').append(report)}load();
</script></html>'''
