"""Mailbox names from preserved source paths, independent of sender addresses."""
from functools import lru_cache
import json
from pathlib import PurePosixPath


BASE = {
    'inbox': ('받은편지함', ('inbox','받은편지함','받은메일함')),
    'sentbox': ('보낸편지함', ('sentbox','보낸편지함','보낸메일함')),
    'selfbox': ('내게쓴편지함', ('selfbox','내게쓴편지함')),
    'tempbox': ('임시보관함', ('tempbox','임시보관함','임시편지함')),
    'spambox': ('스팸편지함', ('spambox','스팸편지함','스팸메일함')),
    'trashbox': ('지운편지함', ('trashbox','지운편지함','휴지통')),
}
ALIASES = {alias: key for key, (_,aliases) in BASE.items() for alias in aliases}


def classify(name, format=None):
    name=str(name or '').replace('\\','/').strip('/')
    parts=list(PurePosixPath(name).parts)
    maildir=False
    if format=='rfc822' or (parts and parts[-1].lower().endswith('.eml')):
        parts=parts[:-1]
        if parts and parts[-1].lower() in ('cur','new','tmp'):
            parts.pop()
            maildir=True
    parts=[part[:-5] if part.lower().endswith('.mbox') else part for part in parts]
    parts=[part for part in parts if part not in ('','.','..')]
    if not parts:
        return {'id':'other:unknown','label':'분류 미확인','category':'other'}
    normalized=parts[0].lower()
    if maildir:
        normalized=normalized.lstrip('.')
    key=None
    if len(parts)==1:
        if normalized in BASE:
            key=normalized
        elif maildir:
            key={'sent':'sentbox','drafts':'tempbox','trash':'trashbox','junk':'spambox'}.get(normalized)
        elif format is None:
            # Older indexes can contain already translated labels. With a
            # recorded source format, only SEGIO's actual reserved names are
            # system folders; a custom folder named "보낸편지함" stays custom.
            key=ALIASES.get(normalized.replace(' ',''))
    if key:
        return {'id':'base:'+key,'label':BASE[key][0],'category':'base'}
    return {'id':'custom:'+'/'.join(parts),'label':' / '.join(parts),'category':'custom'}


class MailFolders:
    def __init__(self,root):
        path=root/'mail/sources.json'
        data=json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
        self.sources={source['name']:source.get('format') for source in data.get('sources',[])
                      if isinstance(source.get('name'),str) and source.get('format') in ('mbox','rfc822')}
        self.info=lru_cache(maxsize=8192)(self._info)

    def _info(self,name):
        try:
            values=json.loads(name)
            if isinstance(values,list) and len(values)==1 and isinstance(values[0],str):
                name=values[0]
        except (ValueError,TypeError):
            pass
        return classify(name,self.sources.get(name))

    def register(self,db):
        db.create_function('mailbox_key',1,lambda name:self.info(name)['id'],deterministic=True)
        db.create_function('mailbox_label',1,lambda name:self.info(name)['label'],deterministic=True)

    def boxes(self,db):
        boxes={}
        def add(info,count=0):
            box=boxes.setdefault(info['id'],dict(info,count=0))
            box['count']+=count
        for key in ('inbox','sentbox'):
            add(classify(key))
        for name in self.sources:
            add(self.info(name))
        for name,count in db.execute("SELECT folders,count(*) FROM items WHERE kind='mail' GROUP BY folders"):
            add(self.info(name),count)
        order={'base':0,'custom':1,'other':2}
        base_order={('base:'+key):i for i,key in enumerate(BASE)}
        return sorted(boxes.values(),key=lambda box:(order[box['category']],base_order.get(box['id'],99),box['label'].casefold()))
