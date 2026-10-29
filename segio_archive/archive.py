from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

STATES = {
    "running": "수집 중", "collected": "수집 종료 · 대조 확인 필요",
    "collected_with_warnings": "수집 종료 · 확인할 항목 있음",
    "partial": "일부 저장 후 오류 발생", "failed": "수집 실패", "cancelled": "수집 중단",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_name(value: str, limit: int = 70) -> str:
    return _filename(value, limit, trim_tail=True)


def legacy_mail_attachment_name(value: str) -> str:
    """Identify names in old mail metadata; never name new attachments with this."""
    return _filename(value, 80, trim_tail=False)


def _filename(value: str, limit: int, *, trim_tail: bool) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:limit]
    if trim_tail:
        value = value.rstrip(" .")
    if not value or value.split(".")[0].upper() in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        value = "file_" + value
    return value


def atomic_write(path: Path, data: bytes) -> None:
    from .file_ops import replace_file
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
        replace_file(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def item_key(kind: str, identity: str) -> str:
    return kind + "-" + hashlib.sha256(identity.encode()).hexdigest()[:24]


class Archive:
    def __init__(self, root: Path, server: str, user: str):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = root / f"{safe_name(user)}_{stamp}_{uuid.uuid4().hex[:6]}"
        self.path.mkdir(parents=True, exist_ok=False)
        self.db = sqlite3.connect(self.path / "catalog.sqlite3")
        self.db.execute("""CREATE TABLE items (
            id TEXT PRIMARY KEY, kind TEXT, title TEXT, folders TEXT,
            source TEXT, body TEXT, html TEXT, pdf TEXT, eml TEXT,
            attachments TEXT, captured_at TEXT, status TEXT)""")
        self.db.execute("CREATE TABLE events (time TEXT, level TEXT, message TEXT)")
        self.manifest = {
            "format": "segio-archive/1", "server": server, "user": user,
            "started_at": utc_now(), "finished_at": None, "state": "running",
            "account_deletion_verified": False,
            "warnings": ["계정 삭제 영향은 검증되지 않았습니다. 이 프로그램은 계정을 삭제하지 않습니다."],
            "coverage": [], "files": {}, "counts": {},
        }
        self.save_manifest()

    def write(self, relative: str, data: bytes) -> str:
        target = (self.path / relative).resolve()
        if not target.is_relative_to(self.path.resolve()):
            raise ValueError("보관 경로를 벗어난 파일명")
        atomic_write(target, data)
        self.manifest["files"][relative] = {
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        }
        return relative

    def event(self, level: str, message: str) -> None:
        self.db.execute("INSERT INTO events VALUES (?,?,?)", (utc_now(), level, message))
        self.db.commit()
        if level in {"warning", "error"} and message not in self.manifest["warnings"]:
            self.manifest["warnings"].append(message)
        self.save_manifest()

    def add(self, *, key: str, kind: str, title: str, folder: str, source: str,
            body: str, html_path: str, pdf: str = "", eml: str = "",
            attachments: list | None = None, status: str = "captured") -> None:
        row = self.db.execute("SELECT folders FROM items WHERE id=?", (key,)).fetchone()
        folders = json.loads(row[0]) if row else []
        if folder not in folders:
            folders.append(folder)
        self.db.execute("""INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (key, kind, title, json.dumps(folders, ensure_ascii=False), source,
                         body, html_path, pdf, eml, json.dumps(attachments or [], ensure_ascii=False),
                         utc_now(), status))
        self.db.commit()

    def add_folder(self, key: str, folder: str) -> bool:
        row = self.db.execute("SELECT folders FROM items WHERE id=?", (key,)).fetchone()
        if not row:
            return False
        folders = json.loads(row[0])
        if folder not in folders:
            folders.append(folder)
            self.db.execute("UPDATE items SET folders=? WHERE id=?",
                            (json.dumps(folders, ensure_ascii=False), key))
            self.db.commit()
        return True

    def save_manifest(self) -> None:
        atomic_write(self.path / "manifest.json",
                     json.dumps(self.manifest, ensure_ascii=False, indent=2).encode())

    def finish(self, state: str) -> None:
        self.manifest["state"] = state
        self.manifest["finished_at"] = utc_now()
        self.manifest["counts"] = dict(self.db.execute(
            "SELECT kind, count(*) FROM items GROUP BY kind").fetchall())
        self.make_index()
        self.db.commit()
        # Checkpoint the DB before hashing. Viewer never needs a running database server.
        self.db.close()
        db_data = (self.path / "catalog.sqlite3").read_bytes()
        self.manifest["files"]["catalog.sqlite3"] = {
            "bytes": len(db_data), "sha256": hashlib.sha256(db_data).hexdigest()}
        self.save_manifest()

    def make_index(self) -> None:
        self.db.row_factory = sqlite3.Row
        items = []
        for row in self.db.execute("SELECT * FROM items ORDER BY kind, title"):
            item = dict(row)
            item["folders"] = json.loads(item["folders"])
            item["attachments"] = json.loads(item["attachments"])
            # This is text in an inert application/json element, never executable HTML.
            items.append(item)
        payload = json.dumps({"items": items, "manifest": self.manifest}, ensure_ascii=False)
        payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        page = INDEX.replace("__DATA__", payload)
        self.write("index.html", page.encode())


def verify_archive(root: Path) -> dict:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    errors = []
    for name, expected in manifest["files"].items():
        p = (root / name).resolve()
        if not p.is_relative_to(root.resolve()) or not p.is_file():
            errors.append({"file": name, "reason": "missing_or_unsafe"})
        else:
            digest, count = hashlib.sha256(), 0
            with p.open('rb') as stream:
                while block := stream.read(1024*1024):
                    digest.update(block)
                    count += len(block)
            if count != expected["bytes"] or digest.hexdigest() != expected["sha256"]:
                errors.append({"file": name, "reason": "checksum_mismatch"})
    return {"ok": not errors, "checked": len(manifest["files"]), "errors": errors,
            "account_deletion_verified": False}


INDEX = r'''<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>SEGIO 문서·메일 보관함</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f2f5f9;color:#162234;font:15px system-ui,sans-serif}
header{padding:28px 5%;background:#172b43;color:white}h1{font-size:24px;margin:0 0 8px}
main{max-width:1160px;margin:24px auto;padding:0 20px}.note{background:#fff5d6;padding:16px;border-radius:8px;white-space:pre-line}
.bar{display:flex;gap:10px;margin:24px 0}input,select{padding:12px;border:1px solid #aebccc;border-radius:6px;font:inherit}input{flex:1;min-width:0}
.card{background:white;border:1px solid #dce3eb;padding:20px;margin:12px 0;border-radius:10px}h2{font-size:18px;margin:0 0 10px;overflow-wrap:anywhere}
.meta{color:#546579;font-size:13px;white-space:pre-line}.links{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}a{color:#075b9c;overflow-wrap:anywhere}a.button{padding:7px 13px;border:1px solid #c6d7e5;border-radius:5px;text-decoration:none}
details{margin-top:14px}#summary{margin:14px 0}footer{padding:24px 0;color:#546579}button{padding:8px 16px;border:1px solid #aebccc;background:white;border-radius:5px;cursor:pointer}
</style><header><h1>문서와 메일, 계정 없이 열람</h1><div id="owner"></div></header>
<main><div class="note" id="notice"></div><div class="bar"><input id="q" placeholder="제목·본문·기안자·메일 주소 검색" aria-label="검색">
<select id="kind" aria-label="종류"><option value="">전체</option><option value="approval">결재 문서</option><option value="mail">메일</option></select></div>
<div id="summary"></div><div id="list"></div><button id="more" hidden>다음 50개 보기</button>
<details><summary>수집 범위·검증 내역</summary><pre id="coverage" style="white-space:pre-wrap;overflow-wrap:anywhere"></pre></details>
<footer>첨부 원본은 해당 파일을 지원하는 Windows 프로그램으로 열립니다. 파일 무결성 검사는 수집 프로그램의 ‘검증’ 기능을 이용하세요.</footer></main>
<script type="application/json" id="data">__DATA__</script><script>
'use strict';const data=JSON.parse(document.getElementById('data').textContent);let shown=50;
const $=id=>document.getElementById(id);$('owner').textContent=data.manifest.user+' · '+data.manifest.server;
const states={running:'수집 중',collected:'수집 종료 · 대조 확인 필요',collected_with_warnings:'수집 종료 · 확인할 항목 있음',partial:'일부 저장 후 오류 발생',failed:'수집 실패',cancelled:'수집 중단'};
const warnings=data.manifest.warnings.slice(1);
$('notice').textContent=(states[data.manifest.state]||data.manifest.state)+'\n계정 삭제 영향 미검증. 오프라인 열람·누락 대조와 별도로 서버 전체 백업 및 삭제 영향 시험이 필요합니다.\n'+warnings.slice(0,4).join('\n')+(warnings.length>4?'\n추가 '+(warnings.length-4)+'개 항목은 아래 수집 범위·검증 내역에서 확인하세요.':'');
$('coverage').textContent=JSON.stringify({수집범위:data.manifest.coverage,확인사항:data.manifest.warnings},null,2);
function link(label,path){const a=document.createElement('a');a.textContent=label;a.className='button';a.target='_blank';a.rel='noopener noreferrer';a.href=path.split('/').map(encodeURIComponent).join('/');return a}
function render(){const q=$('q').value.toLocaleLowerCase();const kind=$('kind').value;
const found=data.items.filter(i=>(!kind||i.kind===kind)&&[i.title,i.body,...i.folders].join(' ').toLocaleLowerCase().includes(q));$('list').replaceChildren();
$('summary').textContent=found.length+'개 중 '+Math.min(shown,found.length)+'개 표시';
for(const i of found.slice(0,shown)){const d=document.createElement('article');d.className='card';const h=document.createElement('h2');h.textContent=i.title;d.append(h);
const meta=document.createElement('div');meta.className='meta';meta.textContent=(i.kind==='mail'?'메일':'결재')+' · '+i.folders.join(', ')+'\n'+new Date(i.captured_at).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'})+' KST'+(i.kind==='mail'&&!i.eml&&!i.mbox?' · 메일 원문 미확보: 화면 보관본':'');d.append(meta);
const links=document.createElement('div');links.className='links';if(i.html)links.append(link('본문 보기',i.html));if(i.pdf)links.append(link('PDF',i.pdf));if(i.eml)links.append(link('메일 원문 EML',i.eml));if(i.mbox)links.append(link('원본 MBOX',i.mbox));if(i.json)links.append(link('원본 DB 기록',i.json));for(const a of i.attachments)links.append(link('첨부: '+a.name,a.path));d.append(links);$('list').append(d)}$('more').hidden=shown>=found.length}
for(const id of ['q','kind'])$(id).addEventListener('input',()=>{shown=50;render()});$('more').onclick=()=>{shown+=50;render()};render();
</script></html>'''
