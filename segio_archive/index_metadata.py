"""Read-only archive dates and approval categories, without capture-time guesses."""
from email.utils import parsedate_to_datetime
from functools import lru_cache
from datetime import datetime, timedelta, timezone
import json

CATEGORIES = {'completed':'완료문서', 'rejected':'반려문서', 'reference':'참조문서',
              'received':'수신문서', 'pending':'진행문서', 'draft':'임시문서', 'unknown':'상태 미확인'}
STATES = {key:key for key in ('completed','rejected','pending','draft')}
STATES.update({'complete':'completed','approved':'completed','완료':'completed','결재완료':'completed',
               '완료문서':'completed','반려':'rejected','반려문서':'rejected','reject':'rejected',
               'returned':'rejected','진행':'pending','진행중':'pending','결재중':'pending',
               '임시':'draft','임시저장':'draft','drafts':'draft'})


def header_date(raw):
    return _header_date(str(raw or '')[:4096])


@lru_cache(maxsize=8192)
def _header_date(raw):
    try:
        value = parsedate_to_datetime(raw)
        if value is None:
            raise ValueError()
        display = value.strftime('%Y-%m-%d %H:%M')
        if value.tzinfo is None:
            return display+' (시간대 미상)', None
        return display+' '+value.strftime('%z'), int(value.timestamp())
    except (ValueError, TypeError, OverflowError, OSError):
        return '날짜 미상', None


def body_date(body):
    # Older personal-login indexes stored From, To, Date, body in this order.
    parts = str(body or '').split('\n', 3)
    raw = parts[2] if len(parts)>2 else ''
    return raw if header_date(raw)[0]!='날짜 미상' else ''


def approval_date(payload):
    """SEGIO uses Unix seconds for document.wdate; never use completion/backup time."""
    try:
        raw = json.loads(payload) if isinstance(payload, str) else payload
        value = raw.get('document', {}).get('wdate')
        if value is None or isinstance(value, bool) or not str(value).strip():
            raise ValueError()
        value = str(value).strip()
        local = timezone(timedelta(hours=9))
        if value.isdigit():
            if int(value) <= 0:
                raise ValueError()
            date = datetime.fromtimestamp(int(value), local)
        else:
            date = datetime.fromisoformat(value.replace(' KST', '+09:00'))
            date = date.replace(tzinfo=local) if date.tzinfo is None else date.astimezone(local)
        return date.strftime('%Y-%m-%d %H:%M:%S KST'), int(date.timestamp())
    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
        return '기안일자 미상', None


def item_date(kind, mail_date, payload):
    return header_date(mail_date)[0] if kind == 'mail' else approval_date(payload)[0]


def members(value):
    return {part.split('@@',1)[0].strip() for part in str(value or '').split(',') if part.strip()}


def approval_categories(payload, user, mapping=None):
    try:
        raw = json.loads(payload) if isinstance(payload,str) else payload
        document = raw.get('document',{})
        if not isinstance(document,dict):
            return ['unknown']
        progress = str(document.get('progress') if document.get('progress') is not None else '').strip().lower().replace(' ','')
        state = (mapping or {}).get(progress, STATES.get(progress,'unknown'))
        if state not in ('completed','rejected','pending','draft'):
            state = 'unknown'
        result = [state]
        if user and user in members(document.get('reference')):
            result.append('reference')
        related = raw.get('related',{})
        received = bool(user and user in members(document.get('doc_recv_uid')))
        if user and isinstance(related,dict):
            for table in ('approval_send_box','approval_send_box_backup'):
                for row in (related.get(table) or []):
                    if isinstance(row,dict) and any(user in members(row.get(key)) for key in ('recv_user','recv_uid')):
                        received = True
        if received:
            result.append('received')
        return result
    except (ValueError, TypeError, AttributeError):
        return ['unknown']


class IndexMetadata:
    def __init__(self, root, db, manifest=None):
        manifest = manifest or json.loads((root/'manifest.json').read_text(encoding='utf-8'))
        self.user = manifest.get('user','')
        self.mapping = manifest.get('approval_status_map',{})
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        joins, dates = [], []
        if 'mail_dates' in tables:
            joins.append('LEFT JOIN mail_dates md ON md.id=i.id')
            dates.append('md.header_date')
        if 'mail_records' in tables:
            joins.append('LEFT JOIN mail_records mr ON mr.id=i.id')
            dates.append('mr.date')
        dates.append('body_date(i.body)')
        self.date_sql = 'COALESCE('+','.join(dates)+")" if len(dates)>1 else dates[0]
        self.payload_sql = "'{}'"
        if 'document_records' in tables:
            joins.append('LEFT JOIN document_records dr ON dr.id=i.id')
            self.payload_sql = "COALESCE(dr.payload,'{}')"
        self.joins = ' '.join(joins)
        db.create_function('body_date',1,body_date,deterministic=True)
        db.create_function('header_epoch',1,lambda value:header_date(value)[1],deterministic=True)
        db.create_function('approval_has',2,lambda value,key:int(key in self.categories(value)),deterministic=True)
        db.create_function('approval_label',1,lambda value:' · '.join(CATEGORIES[key] for key in self.categories(value)),deterministic=True)
        db.create_function('approval_epoch',1,lambda value:approval_date(value)[1],deterministic=True)
        self.order = "i.kind,CASE WHEN i.kind='mail' THEN header_epoch("+self.date_sql+") ELSE approval_epoch("+self.payload_sql+") END DESC,i.rowid"

    def categories(self, payload):
        return approval_categories(payload,self.user,self.mapping)
