"""An origin-isolated popup forwards selections, never file data or deletion authority."""
import json


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
