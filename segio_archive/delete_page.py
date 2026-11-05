"""Selection and deletion controls shared by direct-file and local HTTP indexes."""
SCRIPT=r'''(()=>{
'use strict';
const byId=id=>document.getElementById(id),picked=new Set(),excluded=new Set();
let all=false,selectionTotal=0,selectionFilter='',busy=false,info=null,active=null,refresh=false,bridgeAbort=null;
const filters=()=>({q:query,kind,mailbox:kind==='mail'?mailbox:'',category:kind==='approval'?category:''});
const filterKey=()=>JSON.stringify(filters());
const count=()=>all?Math.max(0,selectionTotal-excluded.size):picked.size;
const chosen=id=>all?!excluded.has(id):picked.has(id);
const form=byId('search-form'),button=document.createElement('button');
button.id='delete-button';button.type='button';button.textContent='삭제';button.disabled=true;form.append(button);
const toolbar=document.createElement('div');toolbar.id='selection-toolbar';
const selectAll=document.createElement('button'),clear=document.createElement('button'),status=document.createElement('span');
selectAll.id='select-matches';selectAll.type='button';selectAll.textContent='검색 결과 전체 선택';
clear.id='clear-selection';clear.type='button';clear.textContent='선택 해제';status.id='selection-count';status.setAttribute('role','status');
toolbar.append(selectAll,clear,status);byId('list-toolbar').after(toolbar);
const label=document.createElement('label'),pageSelect=document.createElement('input');
pageSelect.type='checkbox';pageSelect.id='select-page';pageSelect.setAttribute('aria-label','현재 페이지 전체 선택');label.append(pageSelect);byId('list-head').prepend(label);
const dialog=document.createElement('dialog');dialog.id='delete-dialog';
dialog.innerHTML='<h2>삭제 경고</h2><p id="delete-description"></p><p>선택한 PC 백업 목록과 개별 문서·메일 바로보기·첨부 파일을 삭제합니다. 휴지통으로 이동하지 않습니다. 그룹웨어 서버 원본은 유지합니다. 여러 메일이 공유하는 MBOX 원본은 보존합니다.</p><p id="delete-progress" role="status"></p><progress id="delete-meter" max="100"></progress><div><button id="delete-confirm" type="button">확인</button> <button id="delete-cancel" type="button">취소</button></div>';
document.body.append(dialog);
function progress(message,done,total){byId('delete-progress').textContent=message+(Number.isFinite(done)?' · '+done.toLocaleString()+(Number.isFinite(total)?' / '+total.toLocaleString():''):'');const meter=byId('delete-meter');if(Number.isFinite(total)&&total>0){meter.value=Math.min(100,done/total*100)}else meter.removeAttribute('value')}
function selected(id,on){if(all){on?excluded.delete(id):excluded.add(id)}else{on?picked.add(id):picked.delete(id)}}
function update(){for(const node of document.querySelectorAll('#search-form input,#scope,#search-button,[role=tab],#prev,#next,#page-size,#page-size-input,#page-size-form button')){if(busy){if(node.dataset.deleteDisabled===undefined)node.dataset.deleteDisabled=String(node.disabled);node.disabled=true}else if(node.dataset.deleteDisabled!==undefined){node.disabled=node.dataset.deleteDisabled==='true';delete node.dataset.deleteDisabled}}status.textContent=count().toLocaleString()+'건 선택';button.disabled=busy||!count();selectAll.disabled=busy||!window.archiveLastResults?.total;clear.disabled=busy||!count();const boxes=[...document.querySelectorAll('input.item-select')];for(const box of boxes){box.checked=chosen(box.dataset.id);box.disabled=busy}pageSelect.checked=boxes.length>0&&boxes.every(box=>box.checked);pageSelect.indeterminate=boxes.some(box=>box.checked)&&!pageSelect.checked;pageSelect.disabled=busy||!boxes.length}
window.archiveSelectionRendered=()=>{if(selectionFilter&&selectionFilter!==filterKey()){all=false;picked.clear();excluded.clear();selectionTotal=0}selectionFilter=filterKey();for(const row of document.querySelectorAll('#results article')){if(row.querySelector('.item-select'))continue;const box=document.createElement('input');box.type='checkbox';box.className='item-select';box.dataset.id=row.dataset.identity;box.setAttribute('aria-label','항목 선택 '+(row.querySelector('h2').textContent||''));box.onchange=()=>{selected(box.dataset.id,box.checked);update()};row.prepend(box)}update()};
const originalLoad=load;load=function(){if(selectionFilter&&selectionFilter!==filterKey()){all=false;picked.clear();excluded.clear();selectionTotal=0;selectionFilter=filterKey();update()}return originalLoad()};
pageSelect.onchange=()=>{for(const box of document.querySelectorAll('.item-select'))selected(box.dataset.id,pageSelect.checked);update()};
selectAll.onclick=()=>{all=true;picked.clear();excluded.clear();selectionTotal=window.archiveLastResults.total;selectionFilter=filterKey();update()};
clear.onclick=()=>{all=false;picked.clear();excluded.clear();selectionTotal=0;update()};
async function api(action,data){if(!info){const response=await fetch('api/edit/info');info=await response.json()}if(!info.enabled)throw Error('정상 완료한 개인 로그인 백업을 선택하세요.');const response=await fetch('api/edit/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-Archive-Token':info.token},body:JSON.stringify(data)});if(!response.ok)throw Error('PC 삭제 관리 연결을 확인하세요.');const result=await response.json();if(result.error)throw Error(result.error);return result}
async function upload(create,add){const initial=await create({all,filters:filters(),revision:config.edit_revision}),selection=initial.selection;active=selection;if(!busy)throw Error('선택 전달을 취소했습니다.');let batch=[],done=0;const source=all?excluded:picked;for(const id of source){if(!busy)throw Error('선택 전달을 취소했습니다.');batch.push(id);if(batch.length===500){await add({selection,ids:batch});done+=batch.length;progress('선택 항목 전달 중',done,source.size);batch=[]}}if(batch.length)await add({selection,ids:batch});return selection}
function show(){if(!dialog.open)dialog.showModal();byId('delete-meter').hidden=false;byId('delete-confirm').disabled=true;byId('delete-cancel').disabled=false;byId('delete-cancel').textContent='취소';byId('delete-description').textContent='삭제할 범위와 건수를 확인하는 중입니다.';progress('선택 확인 중')}
async function preview(selection){active=selection;const result=await api('preview',{selection});byId('delete-description').textContent=result.count.toLocaleString()+'건을 이 PC 백업에서 삭제하시겠습니까? 그룹웨어 ID: '+(info.account||'')+' · 백업 폴더: '+(info.folder||'')+'. 확인을 누르면 삭제를 시작합니다.';progress('삭제 확인 대기',result.count,result.count);byId('delete-confirm').disabled=false}
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function bridge(){
show();progress('열린 연결 창에서 PC 관리 프로그램 열기를 누른 뒤 브라우저의 프로그램 열기를 허용하세요. 취소하려면 연결 취소를 누르세요.');
byId('delete-cancel').textContent='연결 취소';
const bytes=new Uint8Array(32);crypto.getRandomValues(bytes);const token=[...bytes].map(n=>n.toString(16).padStart(2,'0')).join('');
const start='search/manage.html#'+new URLSearchParams({archive:location.href.split('#')[0],token});
const popup=window.open(start,'segio-manage-'+token);if(!popup)throw Error('PC 삭제 관리 창을 열 수 있도록 팝업을 허용하세요.');
let targetOrigin=null,readyResolve,readyReject,serial=0,signal=null,aborted=false,connected=false;
const waiting=new Map(),previousSignal=window.segioManageReady;
const ready=new Promise((resolve,reject)=>{readyResolve=resolve;readyReject=reject});
// Observe the rejection even if cancellation precedes the readiness-file read.
ready.catch(()=>{});
bridgeAbort=()=>{aborted=true;readyReject(Error('PC 관리 연결을 취소했습니다.'));for(const item of waiting.values())item.reject(Error('PC 관리 연결을 취소했습니다.'));if(!popup.closed)popup.close()};
window.segioManageReady=data=>{if(data&&data.protocol==='segio-manage-ready/1'&&data.token===token&&(data.state==='failed'||data.state==='ready'&&Number.isInteger(data.port)&&data.port>0&&data.port<=65535))signal=data};
async function readSignal(){await new Promise(resolve=>{const script=document.createElement('script');let timer;const finish=()=>{clearTimeout(timer);script.remove();resolve()};script.onload=script.onerror=finish;timer=setTimeout(finish,3000);script.src='search/manage-'+token+'.js?check='+Date.now();document.head.append(script)})}
const listen=event=>{const data=event.data;if(event.source!==popup||!data||data.token!==token)return;if(data.type==='segio-connect-cancel'&&(event.origin==='null'||event.origin===location.origin)){byId('delete-cancel').click();return}if(event.origin!==targetOrigin)return;if(data.type==='segio-ready')readyResolve();else if(data.type==='segio-reply'){const item=waiting.get(data.id);if(item){waiting.delete(data.id);clearTimeout(item.timer);data.data.error?item.reject(Error(data.data.error)):item.resolve(data.data)}}};
window.addEventListener('message',listen);
function post(action,data){return new Promise((resolve,reject)=>{const id=++serial;const timeout=setTimeout(()=>{waiting.delete(id);reject(Error('선택 항목 전달 응답을 확인하지 못했습니다.'))},30000);waiting.set(id,{resolve,reject,timer:timeout});popup.postMessage({type:'segio-request',token,id,action,data},targetOrigin)})}
let handshakeTimer;
try{
  const deadline=Date.now()+180000;
  while(!signal){
    if(aborted||!busy||popup.closed)throw Error('PC 관리 연결을 취소했습니다. 선택한 항목은 삭제하지 않았습니다.');
    if(Date.now()>=deadline)throw Error('PC 관리 프로그램의 실행을 확인하지 못했습니다. 새 프로그램을 실행한 뒤 보관함 열기로 백업을 열어주세요.');
    await readSignal();
    if(!signal){progress('프로그램 열기 확인을 기다리는 중입니다. 취소한 경우 연결 취소를 누르세요. · 남은 '+Math.max(0,Math.ceil((deadline-Date.now())/1000))+'초');await pause(400)}
  }
  if(aborted||!busy||popup.closed)throw Error('PC 관리 연결을 취소했습니다.');
  if(signal.state==='failed')throw Error(signal.message||'PC 관리 프로그램을 준비하지 못했습니다. 새 프로그램의 보관함 열기를 사용하세요.');
  targetOrigin='http://127.0.0.1:'+signal.port;
  progress('PC 프로그램 실행을 확인했습니다. 삭제 관리 화면을 여는 중입니다.');
  handshakeTimer=setTimeout(()=>readyReject(Error('실행한 PC 관리 프로그램에 연결하지 못했습니다. 새 프로그램의 보관함 열기를 사용하세요.')),30000);
  popup.location.href=targetOrigin+'/'+token+'/bridge/connect';
  await ready;clearTimeout(handshakeTimer);
  const selection=await upload(data=>post('create',data),data=>post('add',data));
  if(aborted||!busy)throw Error('PC 관리 연결을 취소했습니다.');
  await post('open',{selection});connected=true;active=null;dialog.close();byId('notice').textContent='PC 삭제 관리 화면으로 선택 항목을 전달했습니다. 열린 관리 창에서 삭제 건수를 확인하세요.';
}catch(error){
  if(!popup.closed&&!targetOrigin)popup.postMessage({type:'segio-connect-error',token,message:error.message},'*');
  throw error;
}finally{
  clearTimeout(handshakeTimer);bridgeAbort=null;window.removeEventListener('message',listen);
  if(previousSignal===undefined)delete window.segioManageReady;else window.segioManageReady=previousSignal;
  for(const item of waiting.values()){clearTimeout(item.timer);item.reject(Error('연결이 끝났습니다.'))}
  if(!connected&&!popup.closed&&aborted)popup.close();
}
}
button.onclick=async()=>{if(busy||!count())return;busy=true;update();try{if(location.protocol==='file:'){await bridge();busy=false;update();return}show();const selection=await upload(data=>api('create',data),data=>api('add',data));await preview(selection)}catch(error){progress(error.message);byId('delete-meter').hidden=true;byId('delete-cancel').textContent='닫기';byId('delete-cancel').disabled=false;busy=false;update()}};
byId('delete-cancel').onclick=async()=>{if(refresh){location.hash=kind||'all';location.reload();return}if(bridgeAbort){bridgeAbort();active=null;dialog.close();return}busy=false;if(active&&location.protocol!=='file:'){try{await api('cancel',{selection:active})}catch(error){}}active=null;dialog.close();update()};
dialog.addEventListener('cancel',event=>{event.preventDefault();if(!byId('delete-cancel').disabled)byId('delete-cancel').click()});
byId('delete-confirm').onclick=async()=>{if(!active)return;byId('delete-confirm').disabled=true;byId('delete-cancel').disabled=true;try{await api('start',{selection:active,clean_files:true});while(true){await pause(350);const response=await fetch('api/edit/status?'+new URLSearchParams({selection:active}));if(!response.ok)throw Error('삭제 상태 연결을 확인하지 못했습니다. 프로그램에서 완료 상태를 확인하세요.');const state=await response.json();progress(state.phase,state.completed,state.total);if(state.state==='failed')throw Error(state.message);if(state.state==='done'){const warnings=state.result.warnings||[];byId('delete-description').textContent=state.result.deleted.toLocaleString()+'건 삭제 완료'+(warnings.length?' · '+warnings.join(' '):'');progress(warnings.length?'목록 삭제 완료 · 일부 파일 정리 확인 필요':'백업 목록·개별 파일 삭제를 완료했습니다.',state.result.deleted,state.result.deleted);byId('delete-cancel').disabled=false;byId('delete-cancel').textContent='닫기';active=null;refresh=true;all=false;picked.clear();excluded.clear();busy=false;const configResponse=await fetch('search/config.json',{cache:'no-store'});if(configResponse.ok){Object.assign(config,await configResponse.json());refreshBoxes();refresh=false}page=0;load();update();if(!warnings.length){dialog.close();byId('notice').textContent=state.result.deleted.toLocaleString()+'건 삭제 완료. 선택하지 않은 자료는 유지했습니다.'}break}}}catch(error){progress(error.message);byId('delete-cancel').disabled=false;busy=false;update()}};
if(location.protocol!=='file:'){fetch('api/edit/info').then(response=>response.json()).then(async value=>{info=value;if(value.initial&&location.hash==='#delete'){const f=value.initial_filters||{};query=f.q||'';kind=f.kind||'';mailbox=f.mailbox||'';category=f.category||'';q.value=query;byId('scope').value=kind;page=0;locationHash();busy=true;show();load();await preview(value.initial)}}).catch(error=>{byId('notice').textContent=error.message});setInterval(()=>fetch('api/edit/info').catch(()=>{}),60000)}
if(location.protocol==='file:'){byId('notice').append(document.createTextNode(' Windows에서 index.bat로 보관함을 열면 항목 선택 후 삭제 경고 확인 한 번으로 삭제할 수 있습니다.'))}
window.archiveSelectionRendered();
})();'''
