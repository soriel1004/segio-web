"""An origin-isolated popup forwards selections, never file data or deletion authority."""
import json


START_PAGE=r'''<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>PC 삭제 관리 프로그램 연결</title>
<style>body{font:16px 'Malgun Gothic',sans-serif;line-height:1.9;max-width:680px;margin:60px auto;padding:24px}h1{font-size:24px}button{font:inherit;padding:10px 20px;margin:12px 12px 12px 0;cursor:pointer}#manage-launch{background:#075b9c;color:white;border:1px solid #075b9c;border-radius:5px}#manage-state{white-space:pre-line}</style>
<h1>PC 삭제 관리 프로그램을 여는 중입니다</h1>
<p>아래 <b>PC 관리 프로그램 열기</b>를 누른 뒤 브라우저 확인창에서 <b>Python 열기</b> 또는 <b>프로그램 열기</b>를 눌러주세요.</p>
<p id="manage-state" role="status">프로그램 실행이 확인되면 자동으로 삭제 확인 화면을 엽니다. 실행을 취소했다면 연결 취소를 누르세요. 파일은 아직 삭제하지 않습니다.</p>
<button id="manage-launch" type="button">PC 관리 프로그램 열기</button><button id="manage-cancel" type="button">연결 취소</button>
<script>
'use strict';
const fields=new URLSearchParams(location.hash.slice(1)),token=fields.get('token'),archive=fields.get('archive');
const state=document.getElementById('manage-state'),launch=document.getElementById('manage-launch');
let valid=false;try{valid=/^[a-f0-9]{64}$/.test(token)&&new URL(archive).protocol==='file:'&&!!window.opener}catch(error){}
if(!valid){state.textContent='원래 백업 인덱스에서 삭제 버튼을 다시 눌러 연결하세요.';launch.disabled=true}
launch.onclick=()=>{state.textContent='프로그램 열기 확인창에서 열기를 눌러주세요. 취소한 경우 다시 열거나 연결 취소를 누를 수 있습니다.';const uri='segio-backup://open?'+new URLSearchParams({archive,token,port:0});location.href=uri;launch.textContent='프로그램 다시 열기'};
document.getElementById('manage-cancel').onclick=()=>{if(window.opener)window.opener.postMessage({type:'segio-connect-cancel',token},'*');window.close()};
window.addEventListener('message',event=>{const data=event.data;if(event.source!==window.opener||!data||data.token!==token||data.type!=='segio-connect-error')return;document.querySelector('h1').textContent='PC 관리 연결을 완료하지 못했습니다';state.textContent=data.message;launch.disabled=true;document.getElementById('manage-cancel').textContent='닫기'});
</script></html>'''


def page(token):
    return HTML.replace('__TOKEN__',json.dumps(token).replace('<','\\u003c')).encode('utf-8')


HTML=r'''<!doctype html><html lang="ko"><meta charset="utf-8"><title>PC 백업 삭제 관리 연결</title>
<style>body{font:14px 'Malgun Gothic',sans-serif;margin:40px;line-height:1.8}</style>
<h1>PC 백업 삭제 관리 연결</h1><p id="state">인덱스에서 선택한 항목을 전달받고 있습니다. 이 단계에서는 파일을 삭제하지 않습니다.</p>
<script>
'use strict';const token=__TOKEN__;let pending=false;
function reply(id,data){if(window.opener)window.opener.postMessage({type:'segio-reply',token,id,data},'*')}
window.addEventListener('message',async event=>{const data=event.data;if(event.source!==window.opener||event.origin!=='null'||!data||data.token!==token||data.type!=='segio-request'||!['create','add','open'].includes(data.action))return;if(pending){reply(data.id,{error:'선택 전달이 진행 중입니다.'});return}pending=true;try{const response=await fetch(''+data.action,{method:'POST',headers:{'Content-Type':'application/json','X-Bridge-Token':token},body:JSON.stringify(data.data)});if(!response.ok)throw Error('PC 관리 연결을 확인하지 못했습니다.');const result=await response.json();if(result.error)throw Error(result.error);if(data.action==='open'){const target=new URL(result.url);if(target.origin!==location.origin)throw Error('관리 화면 주소가 올바르지 않습니다.');reply(data.id,{opened:true});document.getElementById('state').textContent='선택한 항목의 삭제 건수를 확인하는 화면을 엽니다.';location.replace(target.href)}else{reply(data.id,result)}}catch(error){reply(data.id,{error:error.message})}finally{pending=false}});
if(window.opener)window.opener.postMessage({type:'segio-ready',token},'*');else document.getElementById('state').textContent='원래 인덱스에서 삭제 버튼을 다시 눌러 연결하세요.';
</script></html>'''
