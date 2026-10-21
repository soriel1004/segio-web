"""Update local document presentation while preserving DB records and attachments."""
import base64
import binascii
from contextlib import closing
import hashlib
import html
import json
import re
import sqlite3
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

from .archive import atomic_write

VERSION = 'document-page/1'
MAX_SIGNATURE = 28 * 1024 * 1024


def signature_image(value):
    """Recover raster signatures stored as base64, data URIs or encoded text."""
    value = str(value or '').strip()
    if not value or len(value) > MAX_SIGNATURE:
        return None
    for _ in range(6):
        previous = value
        if value.startswith('"'):
            try:
                decoded = json.loads(value)
                if isinstance(decoded, str):
                    value = decoded
            except ValueError:
                pass
        value = html.unescape(value).replace('\\/', '/')
        # unquote_plus would corrupt literal '+' in ordinary base64 signatures.
        if re.search(r'%[0-9a-fA-F]{2}', value):
            value = unquote(value)
        if value == previous:
            break
    if '<' in value:
        soup = BeautifulSoup(value, 'html.parser')
        candidates = [tag.get('src', '') for tag in soup.find_all('img')]
        candidates.append(soup.get_text())
        return next((image for candidate in candidates if candidate != value and
                     (image := signature_image(candidate))), None)
    if value.lower().startswith('data:'):
        match = re.fullmatch(r'data:image/(?:png|jpeg|gif|webp);base64,(.*)', value, re.I | re.S)
        if not match:
            return None
        value = match[1]
    encoded = re.sub(r'\s+', '', value)
    if not re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', encoded):
        return None
    try:
        data = base64.b64decode(encoded + '=' * (-len(encoded) % 4), validate=True)
    except (ValueError, binascii.Error):
        return None
    mime = ('png' if data.startswith(b'\x89PNG\r\n\x1a\n') else
            'jpeg' if data.startswith(b'\xff\xd8\xff') else
            'gif' if data[:6] in (b'GIF87a', b'GIF89a') else
            'webp' if data[:4] == b'RIFF' and data[8:12] == b'WEBP' else None)
    if not mime or len(data) > 20 * 1024 * 1024:
        return None
    return 'data:image/' + mime + ';base64,' + base64.b64encode(data).decode('ascii')


def encoded_signature(value):
    value = str(value or '').strip()
    return value.lower().startswith('data:image/') or (len(value) > 256 and bool(
        re.fullmatch(r'[A-Za-z0-9+/=%\\\s";:.,_-]+', value)))


def render_document(original, payload):
    soup = BeautifulSoup(original, 'html.parser')
    navigation = soup.select_one('.navigation')
    if navigation:
        for link in navigation.find_all('a', href=True):
            path = urlsplit(link['href']).path
            if path.endswith('/index.html'):
                label, icon = '보관함 목록', '<path d="M8 6h13M8 12h13M8 18h13M3 6h1M3 12h1M3 18h1"/>'
            elif path == 'database.json':
                label, icon = '원본 DB 기록', '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/>'
            else:
                continue
            link.clear()
            link['aria-label'] = link['title'] = label
            link['class'] = ['document-icon']
            graphic = BeautifulSoup('<svg aria-hidden="true" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">'+icon+'</svg>', 'html.parser')
            link.append(graphic.svg)
        style = soup.find('style', id='document-icons') or soup.new_tag('style', id='document-icons')
        style.string = '.document-icon{display:inline-flex;align-items:center;justify-content:center;width:36px;height:36px;border:1px solid #a4b1c2;border-radius:6px;color:#075b9c;vertical-align:middle}.document-icon:hover{background:#e8f1fa}.document-icon:focus-visible{outline:2px solid #075b9c;outline-offset:2px}'
        if style.parent is None:
            (soup.head or soup).append(style)
    for link in soup.find_all('a', href=True):
        if 'attachments' in unquote(urlsplit(link['href']).path).split('/'):
            link['target'], link['rel'] = '_blank', ['noopener', 'noreferrer']

    # Recover signatures from raw DB values; only raster data is ever embedded.
    related = payload.get('related', {})
    source = 'approval_line_backup' if payload.get('source') == 'history' else 'approval_line'
    lines = related.get(source, []) if isinstance(related, dict) else []
    try:
        lines = sorted(lines, key=lambda row: (int(row.get('order_no') or 0), int(row['no'])))
    except (ValueError, TypeError, KeyError):
        lines = []
    table = next((table for table in soup.find_all('table') if any(
        cell.get_text(strip=True) == '서명' for cell in table.find_all('th'))), None)
    unresolved = 0
    if table:
        rows = [row for row in table.find_all('tr', recursive=False) if row.find('td', recursive=False)]
        if not rows:
            rows = [row for row in table.select('tbody > tr') if row.find('td', recursive=False)]
        for number, row in enumerate(rows):
            cell = row.find_all('td', recursive=False)[-1]
            value = lines[number].get('sign') if number < len(lines) else cell.get_text()
            image = signature_image(value)
            if image:
                cell.clear()
                cell.append(soup.new_tag('img', src=image, alt='저장된 서명'))
            elif (encoded_signature(value) or encoded_signature(cell.get_text()) or
                  len(cell.get_text(strip=True)) > 256) and not cell.find('img'):
                cell.clear()
                cell.string = '[서명 이미지 미확보]'
                unresolved += 1

    # Some forms put the same encoded signature in a body text node.
    for node in list(soup.find_all(string=True)):
        if node.parent.name in ('script', 'style', 'title') or len(str(node).strip()) < 32:
            continue
        image = signature_image(str(node))
        if image:
            node.replace_with(soup.new_tag('img', src=image, alt='저장된 서명'))
    for image in soup.find_all('img'):
        if len(str(image.get('alt', ''))) > 256:
            image['alt'] = '저장된 이미지'
    return str(soup).encode('utf-8'), unresolved


def upgrade_documents(root, manifest, checkpoint=lambda: None):
    if manifest.get('document_presentation', {}).get('version') == VERSION:
        return
    files = manifest['files']
    unresolved = 0
    changed = False
    try:
        with closing(sqlite3.connect((root/'catalog.sqlite3').as_uri()+'?mode=ro', uri=True)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'document_records' not in tables:
                return
            for name, payload in db.execute("SELECT i.html, d.payload FROM items i JOIN document_records d ON d.id=i.id WHERE i.kind='approval'"):
                checkpoint()
                path = (root/name).resolve()
                if not path.is_relative_to(root) or not name.startswith('documents/') or name not in files:
                    raise ValueError('결재 화면의 검증된 로컬 경로를 확인하지 못했습니다.')
                original = path.read_bytes()
                expected = files[name]
                if len(original) != expected['bytes'] or hashlib.sha256(original).hexdigest() != expected['sha256']:
                    raise ValueError('결재 화면이 변경되었습니다. 파일 무결성을 확인하세요.')
                updated, missing = render_document(original, json.loads(payload))
                unresolved += missing
                if updated != original:
                    atomic_write(path, updated)
                    files[name] = {'bytes': len(updated), 'sha256': hashlib.sha256(updated).hexdigest()}
                    changed = True
        manifest['document_presentation'] = {'version': VERSION, 'unresolved_signatures': unresolved}
        changed = True
    finally:
        # Keep completed presentation updates verifiable even if cancelled later.
        if changed:
            atomic_write(root/'manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'))
