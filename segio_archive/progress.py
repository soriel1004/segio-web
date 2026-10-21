"""Measured stage progress, throttled before crossing the GUI thread queue."""
import time

LABELS = {
    'login': '그룹웨어 로그인', 'queued': '서버 추출 준비',
    'database': '서버 결재 문서 추출', 'mail': '서버 메일 복사',
    'packing': '서버 전송 ZIP 생성', 'hashing': '서버 ZIP 검증',
    'download': 'PC로 다운로드', 'verify': '압축 해제·원본 검증',
    'index': '메일 검색 색인 생성', 'render': '메일 본문·첨부 생성',
    'finalize': '백업 폴더 마무리',
}


class ProgressReporter:
    def __init__(self, callback=None):
        self.callback = callback
        self.last_key = None
        self.last_time = 0

    def __call__(self, stage, completed=None, total=None, unit='bytes', **extra):
        if not self.callback or stage not in LABELS:
            return
        completed = completed if type(completed) is int and completed >= 0 else None
        total = total if type(total) is int and total >= 0 else None
        if total is not None and completed is not None and completed > total:
            total = None
        key = (stage, extra.get('attempt'), total)
        now = time.monotonic()
        if key != self.last_key or now - self.last_time >= .25 or (total is not None and completed == total):
            self.callback({'stage': stage, 'completed': completed, 'total': total,
                           'unit': unit if unit in ('bytes', 'files', 'messages') else 'files',
                           'attempt': extra.get('attempt')})
            self.last_key, self.last_time = key, now


def describe_progress(event):
    label = LABELS.get(event['stage'], '백업 진행 중')
    if type(event.get('attempt')) is int and 2 <= event['attempt'] <= 3:
        label += ' · 변경 파일 재시도 %d/2' % (event['attempt'] - 1)
    done, total = event['completed'], event['total']
    percent = done * 100 / total if done is not None and total else None
    if done is not None:
        if event['unit'] == 'bytes':
            size = lambda value: '%.2f GiB' % (value / 1024**3) if value >= 1024**3 else '%.1f MiB' % (value / 1024**2)
            label += ' · ' + size(done) + (' / ' + size(total) if total is not None else ' 처리')
        else:
            label += ' · {:,}'.format(done) + (' / {:,}'.format(total) if total is not None else '') + ('건' if event['unit']=='messages' else '개 파일')
    if percent is not None:
        label += ' (%.1f%%)' % percent
    elif done is None:
        label += ' · 처리량 확인 중'
    return label, percent
