"""Synthetic database adapter for real PHP + subprocess + mailbox integration tests."""
import hashlib
import json
from pathlib import Path
import zipfile


def synthetic_export(config, username, output, **kwargs):
    assert kwargs.get('live_scope') is True
    records = json.loads(Path(config).read_text(encoding='utf-8'))
    record = records[username]
    documents = record.get('documents',[record])
    since = kwargs.get('approval_since')
    if since is not None:
        documents = [item for item in documents if str(item.get('wdate','')).isascii() and
                     str(item.get('wdate','')).isdigit() and len(str(item.get('wdate','')))<=12 and
                     int(item['wdate'])>=since]
    folder = Path(output)/'database'
    folder.mkdir()
    data = {'index.html':b'<p>DB import pending</p>'}
    items = []
    for document in documents:
        identity=str(document.get('no','1'))
        html='documents/'+identity+'/document.html'
        raw='documents/'+identity+'/database.json'
        data[html]=('<meta charset="utf-8"><p>'+document['title']+'</p>').encode()
        data[raw]=json.dumps(document,ensure_ascii=False).encode()
        items.append({'id':identity,'title':document['title'],'source':'active',
                      'path':html,'json':raw,'search':document['title'],'attachments':[]})
    data['catalog.json']=json.dumps({'items':items},ensure_ascii=False).encode()
    manifest={'format':'segio-db-html/1','user':username,'server':'cell_000',
              'state':'collected_with_warnings','account_deletion_verified':False,
              'counts':{'approval':len(items)},'coverage':[],'warnings':['삭제 영향 미검증'],
              'snapshot_rows':{'approval_document':len(items)},
              'files':{name:{'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()} for name,body in data.items()}}
    for name,body in data.items():
        target=folder/name
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(body)
    (folder/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False),encoding='utf-8')
    archive=Path(output)/'intermediate.zip'
    with zipfile.ZipFile(archive,'w'):
        pass
    return str(folder),str(archive)
