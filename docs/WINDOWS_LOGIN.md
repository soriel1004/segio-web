# Windows 로그인으로 현재 DB·MBOX 가져오기

SSH 백업 프로그램과 함께 사용한다면 [공용 tools 안내](../tools/README.md)에 따라 두 프로그램 폴더에 같은 도구 묶음을 적용하세요. 서버 설치를 갱신해도 기존 사용자 지정 메일 경로와 작업 파일은 유지하며, SSH 백업은 별도 비공개 임시 작업을 사용합니다.

Windows에서 **직원의 기존 SEGIO ID·암호**를 입력하면, 인증된 직원 ID로 `cell_000`을 새로 조회하고 그 직원의 MBOX 폴더 전체를 복사합니다. Windows에는 개인 결재 자료와 검색 DB(`catalog.sqlite3`), 원본 메일 파일, 오프라인 열람 화면을 만듭니다. 결재·메일 웹페이지를 순회하지 않으며 브라우저는 기존 SEGIO 로그인에만 사용합니다.

관리자는 연결 기능을 **서버에 한 번 설치**합니다. 직원별 ZIP을 미리 만들거나 자료가 바뀔 때마다 서버에서 추출 명령을 실행할 필요가 없습니다. 기존 `prepare_login_archive.py`와 `bridge.php`는 이전 버전 호환용이며 새 Windows 기본 모드는 사용하지 않습니다.

## 서버에 한 번 설치

[최신 프로젝트 ZIP](https://github.com/soriel1004/segio/archive/refs/heads/main.zip)을 풀고 **`tools` 폴더 전체**를 `/root/tools`에 복사합니다. GitHub HTML 페이지를 스크립트로 저장하지 않습니다. CentOS 6 기본 Python 2.6과 기존 `psql`을 사용하며 추가 Python 패키지 설치는 필요하지 않습니다.

웹 서비스의 **Linux 실행 계정**을 확인합니다. 직원 SEGIO ID나 PostgreSQL 계정과는 다릅니다.

```bash
ps -eo user,comm | awk '$2 ~ /shttpd|httpd|php/ {print}'
```

실제 실행 계정이 `segio`인 경우 root로 다음을 실행합니다. 다른 비-root 계정이면 `--web-user`를 바꿉니다.

```bash
cd /root/tools
python ./install_live_archive.py --web-user segio \
  --config /home/segio/cell000/etc/db.conf
```

`Installed live employee DB/MBOX export: /segio/archive/live.php`가 나오면 설치가 끝났습니다. 설치만으로 직원 자료를 추출하지 않습니다. 업데이트할 때도 최신 `tools` 전체를 복사한 뒤 같은 명령을 실행합니다. 진행 중인 다운로드가 없는 시간에 업데이트합니다.

v0.3.1부터 비공개 설정의 기본 위치는 셀의 `etc/segio-live-archive`입니다. 운영 서버에서는 이전 기본값 `/var/lib/segio-live-archive`의 Linux 읽기 권한과 JSON이 정상이어도 PHP에서 `service_not_configured`가 반환됐고, 셀의 비공개 `etc` 경로에 설치한 뒤 인증된 `capabilities` 응답이 확인됐습니다. 특정 PHP 제한 설정의 이름까지 확인한 것은 아닙니다. 이전 설치를 같은 위치로 맞추려면 다음 명령을 실행합니다. 기존 비공개 폴더는 자동 삭제하지 않습니다.

```bash
python ./install_live_archive.py --web-user segio \
  --config /home/segio/cell000/etc/db.conf \
  --private-root /home/segio/cell000/etc/segio-live-archive
```

설치 후 아래의 **연결 확인과 오류** 주소에서 정상 로그인 사용자의 `protocol`과 `capabilities`가 나오는지 먼저 확인합니다. 설치 메시지는 파일 설치 결과이며 네이티브 PHP에서 실제 설정을 읽을 수 있다는 검증을 대신하지 않습니다.

메일 경로 기본값은 사용자가 알려준 실제 경로에 맞춘 다음 규칙입니다.

```text
/home/segio/cell000/home/{initial}/{user}/mbox
koro → /home/segio/cell000/home/k/koro/mbox
```

`{user}`는 서버가 인증한 정확한 직원 ID, `{initial}`은 그 첫 글자입니다. 다른 셀에서는 지정한 `db.conf`의 셀 경로를 기준으로 같은 규칙을 적용합니다. 규칙이 다르면 설치할 때 `--mail-template '/실제경로/{initial}/{user}/mbox'`를 지정합니다. Windows가 대상 ID나 파일 경로를 서버에 지정하는 기능은 없습니다.

설치 결과:

- 셀의 `folder/html/segio/archive/live.php`: 인증 확인, 추출 요청·진행 조회·다운로드.
- `/home/segio/cell000/etc/segio-live-archive/code`: root 소유 고정 추출 코드. 웹 실행 그룹은 읽기만 가능.
- `/home/segio/cell000/etc/segio-live-archive/config.json`: 기존 DB 설정 **경로**, 메일 경로 규칙, 요청 검증용 비밀값. DB 암호는 복사하지 않음.
- `/home/segio/cell000/etc/segio-live-archive/jobs`: 웹 실행 계정만 접근하는 임시 전송 작업. 직원별 사전 보관본 저장소가 아님.

작업기는 기존 웹 Linux 계정으로 실행합니다. 이 계정이 기존 `db.conf`·문서 첨부·본인 메일 폴더를 읽을 수 있어야 하며 PHP의 `proc_open`을 사용할 수 있어야 합니다. root 웹 서비스는 지원하지 않습니다. 설치기는 기존 파일 권한·DB 암호·`pg_hba.conf`·메일 설정을 변경하거나 sudo 권한을 추가하지 않습니다. PostgreSQL을 Windows에 공개할 필요도 없습니다.

## Windows에서 가져오기

1. **[Windows v0.3.2 직접 다운로드](https://github.com/soriel1004/segio/actions/runs/39390341785/artifacts/12248804934)**로 `SEGIOArchive-Windows`를 GitHub 로그인 후 받습니다. 새 폴더에 압축을 모두 풀고, 안에 ZIP이 또 있으면 그것도 푼 뒤 `SEGIOArchive.exe`를 실행합니다. `_internal` 등 함께 들어 있는 폴더를 유지합니다. 창 제목 **그룹웨어 문서 및 메일 백업 및 검색 · v0.3.2**를 확인합니다. 소스 ZIP은 Python 3.12 64비트 설치 후 `run_windows.bat`을 실행합니다.
2. 방식은 **DB·MBOX 바로 가져오기**로 둡니다. **결재 문서**, **메일**은 기본으로 둘 다 선택되며 하나만 가져올 수도 있습니다.
3. 기존 SEGIO 로그인 주소, 직원 ID(예: `koro`), 그 직원의 SEGIO 암호, 저장 폴더를 입력합니다. DB 암호나 서버 관리자 암호는 입력하지 않습니다.
4. 기존 HTTP 주소를 사용할 경우 **암호화되지 않은 HTTP 연결 허용**을 선택합니다. HTTPS에서는 인증서 검증을 유지합니다.
5. **로그인하고 수집 시작**을 누릅니다. 인증 확인 → 현재 DB 조회 → MBOX 전체 복사 → 다운로드·해시 검증 → Windows 색인 순서로 진행합니다.
6. **보관함 열기**에서 검색창에 **메일 제목**을 입력합니다. 종류를 `mail`로 선택하면 메일만 볼 수 있고 보낸 사람·받는 사람·본문도 검색합니다.

저장 이름은 **`segio-koro.zip`**, **`segio-koro`**입니다. 기존 이름이 있으면 `-2`, `-3`을 붙입니다. 다시 실행하면 당시 DB와 MBOX를 새로 가져옵니다. 암호·쿠키는 보관본이나 설정 파일에 저장하지 않습니다. 직접 로그인 옵션도 실제 인증 ID와 입력 ID가 다르면 거부합니다.

v0.3.1은 SEGIO의 대문자 `UID`/`PASSWD`, 숨은 응답 프레임과 직접 로그인 중 바뀌는 폼 목적지를 지원합니다. 직접 로그인은 비밀번호 입력란이 사라졌는지가 아니라 서버의 네이티브 인증 ID가 입력 ID와 일치하는지로 완료를 판단합니다. 화면 전환 중 프레임이 분리되거나 로그인 팝업이 닫혀도 다른 로그인 창이 유지되면 확인을 계속합니다. 설정 응답 실패·JSON 오류·연결 시간 초과는 단계와 고정 오류 코드로 안내하며 응답 본문·암호·쿠키는 오류에 넣지 않습니다.

v0.3.2는 인증된 직원의 결재 프로필·관련 문서가 모두 없더라도 결재 0건을 경고로 기록하고 메일 수집을 계속합니다. 관리자 CLI의 잘못된 ID 검사와 DB 접속·조회 실패는 여전히 실패로 처리합니다. DB 조회 실패를 결재 0건으로 간주하지 않습니다.

기본 로그인 주소:

```text
http://lssgroup.co.kr/icons/app/cms/?html=/home/login.html&shell=/index.shell:483
```

사내에서는 실제 로그인 주소의 호스트를 `192.168.0.100`으로 바꿀 수 있습니다. 전송이 암호화되지 않는 기존 HTTP 서비스는 신뢰하는 사내망에서 사용합니다.

## Windows에 보관되는 자료

```text
segio-koro/
  index.html                     결재·메일 제목/본문 검색
  catalog.sqlite3                개인 검색 DB와 결재 원본 기록
  catalog.json                   열람 목록
  manifest.json                  수집 범위·건수·크기·SHA-256
  documents/.../document.html     결재 본문·결재선
  documents/.../database.json     원래 결재 DB 값
  documents/.../attachments/...   결재 첨부
  mail/sources.json              원래 메일 경로와 보관 파일의 대응·해시
  mail/raw/...                   MBOX 폴더의 모든 일반 파일 원본
  mail/messages/...              메일별 화면·해제한 첨부
  mail/index-issues.json          메일 형식·대용량 색인 확인 사항
```

MBOX 폴더 안의 모든 일반 파일과 하위 폴더 파일을 **내용을 바꾸지 않고** 가져옵니다. Windows 파일명 제약 때문에 보관 이름은 번호를 사용하며 원래 상대 경로·파일명 바이트는 `sources.json`에 남습니다. 표준 MBOX는 `.mbox`, 개별 RFC822 메일은 `.eml`, 나머지는 `.bin`으로 보존합니다. 심볼릭 링크·특수 파일은 따라가지 않고 실패로 표시합니다. 복사 도중 메일이 변경·추가·삭제되면 완료로 처리하지 않으므로 변경이 적은 시간에 재시도합니다.

표준 MBOX/RFC822의 UTF-8·EUC-KR·CP949 제목과 MIME 본문·첨부를 색인합니다. 인식하지 못하는 고유 형식 파일도 원본은 보존하며 `index-issues.json`에 기록합니다. **실제 서버의 메일 파일 형식은 아직 대조하지 않아 모든 고유 형식의 제목 해독까지 검증한 상태는 아닙니다.** 개별 메일이 32 MiB보다 크면 제목·헤더만 색인하며 전체 본문·첨부는 원본 MBOX/EML에 보존합니다. 서버 원본은 수정하지 않습니다.

`cell_000`은 직원 공용 DB이므로 전체 DB를 직원 PC에 복제하지 않습니다. 인증 ID가 기안자·결재선·대결자·참조·자동회람·개인 수신자로 확인되는 결재와 해당 문서의 관련 기록·이력을 한 읽기 전용 DB 스냅샷으로 가져옵니다. 댓글·즐겨찾기·내부 저장 기록만으로 연결된 문서는 대상 선정에서 제외합니다. **이 범위를 직원에게 제공하도록 관리자가 설치하는 방식이며, 인코딩된 SEGIO 원본의 화면별 권한 판정과 동일하다고 검증한 기능은 아닙니다.** 부서 권한만으로 열리는 문서와 별도 확장 업무는 범위에 포함하지 않습니다.

DB 조회 뒤 첨부·메일을 복사하므로 DB와 파일의 동일 시점을 보장하는 전체 서버 백업은 아닙니다. 결재 PDF 자동 생성은 이 모드에 없으며 HTML을 인쇄해 PDF로 저장할 수 있습니다.

## 연결 확인과 오류

로그인한 브라우저에서 아래 주소를 열면 `protocol: segio-live-archive/1`, 본인의 `username`, `capabilities`가 나옵니다. 응답의 `csrf`는 전송하지 마세요.

```text
http://192.168.0.100/segio/archive/live.php?action=info
```

| 결과 | 조치 |
| --- | --- |
| 미설치 / 404 / HTML 응답 | 최신 설치 명령 결과와 셀 웹 경로·PHP 지원 확인 |
| `service_not_configured` | 비공개 `config.json` 읽기 권한·JSON 필수 항목·PHP 경로 제한 확인. 이전 `/var/lib` 설치는 위의 셀 `etc` 경로로 재설치. 권한 전체 개방이나 PHP 제한 전체 해제로 해결하지 않음 |
| `BRIDGE_INVALID_JSON` / `BRIDGE_NOT_JSON` | 새 PHP 경로가 실제 실행되는지와 PHP 오류 확인. 응답 원문·직원 자료를 보내지 말고 오류 코드만 전달 |
| `MANUAL_LOGIN_TIMEOUT` / `LOGIN_NOT_CONFIRMED` | 같은 서버에 로그인했는지, 확인 URL의 네이티브 사용자와 Windows 입력 ID가 같은지 확인 |
| `native_login_required` / `identity_conflict` | 기존 SEGIO 인증 `REMOTE_USER`가 새 PHP 경로에 전달되는지 확인. HTTP 헤더나 입력 ID를 인증값으로 대체하지 않음 |
| `export_process_unavailable` | 웹 실행 계정의 고정 코드 읽기·Python 실행 권한, PHP `proc_open` 확인 |
| `db_authentication_failed` / `db_connection_failed` | 기존 db.conf의 DB 인증 또는 로컬 PostgreSQL 연결 확인 |
| `db_permission_denied` / `db_schema_mismatch` | DB 역할의 읽기 권한 또는 실제 결재 테이블·컬럼 구조 확인 |
| `server_access_denied` / `server_disk_full` | 표시된 실패 단계의 파일 접근 권한 또는 서버 작업 디스크 여유 확인 |
| `export_failed` | 아래 진단으로 최근 오류 단계 확인. v0.3.2 서버 도구도 함께 설치 |

### 로그인 직후 추출 실패 진단

Windows 프로그램은 성공·실패 작업의 임시 폴더를 정리하므로 종료 후 `jobs/*/status.json`이 없는 것만으로 작업이 실행되지 않았다고 판단할 수 없습니다. v0.3.2부터 `jobs/last-failure.json`에 최근 오류 코드·단계·분류만 남깁니다. 직원 ID·암호·DB 설정값·파일 경로·SQL·문서·메일 내용은 기록하지 않습니다. 이 파일은 웹에 공개하지 않습니다.

최신 `tools` 전체를 복사하고 위의 설치 명령을 다시 실행한 뒤, root에서 다음을 실행합니다.

```bash
cd /root/tools
python ./diagnose_live_archive.py --web-user segio \
  --private-root /home/segio/cell000/etc/segio-live-archive
```

진단은 권한을 실제 웹 OS 계정으로 낮춘 뒤 설치된 도구를 실행합니다. 임시 암호 파일을 자동 삭제하며, 기존 DB 연결로 읽기 전용 트랜잭션 안에서 14개 대상 테이블의 허용 컬럼·권한과 실제 조회 SQL의 `EXPLAIN`을 검사합니다. 직원 자료를 조회·추출하지 않고 결과는 고정 검사 항목과 오류 코드만 출력합니다. `database_connection_and_schema: ok`이면 연결·구조 검사는 통과한 것이며 실제 직원 자료 추출 완료를 뜻하지는 않습니다.

CentOS 6 기본 Python 2.6에는 `os.initgroups`가 없습니다. 최신 진단 도구는 `grp`와 `os.setgroups`로 웹 계정의 그룹을 설정한 뒤 GID·UID를 낮춥니다. 이전 도구에서 `AttributeError: ... initgroups`가 발생했다면 Windows 프로그램을 다시 받을 필요 없이 아래 명령으로 즉시 진단할 수 있습니다. 운영체제의 `su`가 먼저 계정을 전환하므로 Python의 root 전환 분기를 거치지 않습니다.

```bash
su -s /bin/sh segio -c '/usr/bin/python /home/segio/cell000/etc/segio-live-archive/code/diagnose_live_archive.py --private-root /home/segio/cell000/etc/segio-live-archive'
```

개인 메일 경로·읽기 권한까지 확인하려면 직원 ID를 직접 입력합니다. 암호는 입력하지 않습니다. 파일 내용은 출력·보관하지 않습니다.

```bash
read -r -p "SEGIO login ID: " SEGIO_TARGET_ID
python ./diagnose_live_archive.py --web-user segio \
  --private-root /home/segio/cell000/etc/segio-live-archive \
  --user "$SEGIO_TARGET_ID"
```

기존 `db.conf`·`psql`의 읽기·실행 권한과 `jobs` 쓰기 검사가 모두 성공해도 DB 인증·테이블 권한·구조·해당 직원 MBOX 접근까지 검증된 것은 아닙니다. 진단의 한 줄 JSON 결과만 전달하면 됩니다.
| `mail_path_missing_or_symlink` | 본인의 실제 MBOX 경로·규칙·심볼릭 링크 여부 확인 |
| `mail_changed_retry` | 복사 중 메일 변경. 변경이 적은 시간에 다시 실행 |
| `export_already_running` / `export_busy` | 같은 직원은 한 작업, 전체는 네 작업까지 실행. 완료 후 재시도 |
| `csrf_required` / 세션 만료 | 새로 로그인해 재시도 |
| 해시 불일치 / 전송 중단 | 완료로 저장하지 않음. 다시 실행 |

임시 ZIP은 전달용입니다. 다운로드·검증이 끝나거나 사용자가 취소하면 본인 작업 정리를 요청합니다. 정리가 실패한 완료·실패 작업은 **다음 추출 요청 때** 24시간이 지난 항목을 삭제합니다. 작업기가 강제 종료돼 실행 중 상태로 남으면 관리자가 프로세스 종료를 확인한 뒤 해당 임시 작업을 정리해야 합니다. 원본 DB·메일을 지우는 기능은 없습니다.

네이티브 `$_ENV['REMOTE_USER']` 또는 `$_SERVER['REMOTE_USER']`만 인증값으로 사용합니다. 임의 대상 ID·SQL·명령·경로를 받지 않으며 POST에는 로그인 사용자에 결합한 CSRF 검증을 적용합니다. 다른 직원의 작업 번호로 조회·다운로드·정리는 할 수 없습니다.

운영 서버의 인증 전달과 실제 문서·메일 형식은 설치 후 현장 확인이 필요합니다. 오류가 나면 **오류 코드만** 전달하세요. 암호·쿠키·실제 보관 자료를 업로드할 필요는 없습니다. 계정 삭제 전에는 네트워크를 끊고 대표 문서·메일 제목·본문·첨부를 대조하고 서버 전체 백업을 별도로 확보합니다.
