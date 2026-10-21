# -*- coding: utf-8 -*-
"""Synthetic end-to-end exporter fixture, including actual Python 2.6."""
from __future__ import print_function
import hashlib
import json
import os
import sys

TOOLS = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'tools'))
sys.path.insert(0, TOOLS)
import export_approval_db as exporter


def main(root):
    cell = os.path.join(root, 'cell')
    upload = os.path.join(cell,'data','approval','upload')
    os.makedirs(upload)
    output = os.path.join(root,'output')
    os.mkdir(output)
    names = [(u'UTF8_\ud55c\uae00+\ucca8\ubd80.txt','utf-8'),
             (u'CP949_\ud55c\uae00+\ucca8\ubd80.txt','cp949')]
    attachments = []
    for number,(name,codec) in enumerate(names):
        native = (upload.encode('utf-8') if not isinstance(upload,bytes) else upload)
        with open(os.path.join(native,name.encode(codec)),'wb') as stream:
            stream.write(b'SYNTHETIC_ATTACHMENT_KEPT_EXACTLY_'+str(number).encode('ascii'))
        attachments.append({'no':str(number+1),'doc_no':'1','fname':name,'fpath':name})
    image_dir = os.path.join(cell,'data','approval','app_images')
    os.mkdir(image_dir)
    image_name = u'\ud55c\uae00+\uc774\ubbf8\uc9c0.png'
    image_native = image_dir.encode('utf-8') if not isinstance(image_dir,bytes) else image_dir
    with open(os.path.join(image_native,image_name.encode('cp949')),'wb') as stream:
        stream.write(b'\x89PNG\r\n\x1a\nSYNTHETIC_RASTER')
    try:
        from urllib import quote
    except ImportError:
        from urllib.parse import quote_from_bytes as quote
    context = (u'<p>\ud55c\uae00 \uacb0\uc7ac \ubcf8\ubb38</p><img src="data/approval/app_images/'+
               quote(image_name.encode('cp949'))+u'">')
    with open(os.path.join(TOOLS,'approval_db_columns.json'),'rb') as stream:
        model = json.loads(stream.read().decode('utf-8'))
    rows = dict((table,[]) for table in model)
    rows['approval_document'] = [{'no':'1','uid':'employee','title':u'\ud55c\uae00 \ud569\uc131 \ubb38\uc11c',
        'context':context,'wdate':'1700000000','progress':u'\uc644\ub8cc'}]
    rows['approval_member_info'] = [{'uid':'employee','rname':u'\uc9c1\uc6d0'}]
    rows['approval_attach_info'] = attachments
    rows['approval_line'] = [{'no':'1','p_no':'1','order_no':'1','uid':'employee','rname':u'\uc9c1\uc6d0',
                             'sign':u'<img src="data/approval/app_images/'+quote(image_name.encode('cp949'))+u'">'}]
    counts = dict((table,len(values)) for table,values in rows.items())
    def query(*args,**kwargs):return rows,counts
    exporter.query_database = query
    folder,archive = exporter.run_export('unused','employee',output,cell=cell,live_scope=True)
    with open(os.path.join(folder,'manifest.json'),'rb') as stream:
        manifest = json.loads(stream.read().decode('utf-8'))
    for name,record in manifest['files'].items():
        with open(os.path.join(folder,name),'rb') as stream:
            body = stream.read()
        assert record['bytes'] == len(body)
        assert record['sha256'] == hashlib.sha256(body).hexdigest()
    with open(os.path.join(folder,'documents/1/document.html'),'rb') as stream:
        page = stream.read().decode('utf-8')
    assert u'\ud55c\uae00 \uacb0\uc7ac \ubcf8\ubb38' in page
    assert page.count('data:image/png;base64,') == 2
    assert all(item.get('kind') != 'attachment_missing_or_unsafe' for item in json.loads(
        open(os.path.join(folder,'issues.json'),'rb').read().decode('utf-8')))
    print(json.dumps({'ok':True,'python':sys.version.split()[0],
                      'filesystem_encoding':sys.getfilesystemencoding(),'attachments':len(attachments)}))


if __name__ == '__main__':main(sys.argv[1])
