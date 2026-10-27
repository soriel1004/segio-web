# 두 프로그램 공용 tools

**[공용 tools ZIP 바로 다운로드](https://github.com/soriel1004/segio/raw/refs/heads/main/downloads/segio-shared-tools.zip)**

이 폴더 전체를 **segio의 SSH 백업 프로그램(v0.4.10)**과 **segio-web의 개인 SEGIO 로그인 프로그램**에 함께 사용할 수 있습니다. SSH 업로드 파일 이름과 두 통신 규약(`segio-ssh-archive/1`, `segio-live-archive/1`)은 유지합니다. 프로그램 본체와 `_internal` 폴더는 각 프로그램의 것을 사용하세요.

## SSH v0.4.5 원본 검사 재시도·진행률

SSH 작업기는 MBOX를 복사하지 않고 원본을 해시하며 용량·파일 수를 보고합니다. 목록이 바뀌면 최대 3회 대조하고 그대로인 파일의 해시를 재사용합니다. SSH 파일 업로드 목록·통신 규약은 유지하므로 기존 SSH 클라이언트와도 호환됩니다. 최신 Windows 프로그램은 자체 `tools`를 비공개 작업 폴더에 올리므로 SSH 사용을 위해 웹 연결 기능을 설치할 필요가 없습니다.

아래 v0.6.x 기능은 개인 로그인 프로그램의 웹 추출 요청용입니다. SSH v0.4.5는 전체 메일과 동일 작업 이어받기를 지원합니다.

<a id="approval-date-install"></a>

## v0.6.7 기안일 기준 새 결재 추출

Windows v0.6.7의 추가 백업은 마지막 **결재** 백업 기준 시각 이후 기안한 새 문서만 서버에서 추출합니다. 원본 `approval_document.wdate`와 과거 문서의 기안일을 사용하고, 해당 문서에 연결된 결재선·첨부·이력을 함께 반환합니다. 기준 시각 전에 기안한 문서의 이후 완료·반려·본문·첨부 변경은 다시 가져오지 않습니다. 기안일 미확인 문서는 날짜 제한 추출에서 제외하며 전체 백업에서는 유지합니다. 별도 SSH 프로그램은 기존처럼 전체 결재를 추출합니다.

Windows 프로그램과 서버 공용 tools를 모두 갱신합니다. 진행 중인 두 프로그램의 백업이 끝난 뒤 root SSH에서 아래 명령을 실행하세요. 그룹웨어 재부팅은 필요하지 않습니다.

```bash
TOOLS_DIR=$(mktemp -d /root/segio-tools.XXXXXX) &&
curl -fL https://raw.githubusercontent.com/soriel1004/segio-web/main/downloads/segio-web-0.6.7.zip \
  -o "$TOOLS_DIR/segio-web-0.6.7.zip" &&
unzip -q "$TOOLS_DIR/segio-web-0.6.7.zip" -d "$TOOLS_DIR" &&
cd "$TOOLS_DIR/segio-web-0.6.7/tools" &&
/usr/bin/python ./check_shared_tools.py &&
/usr/bin/python ./install_live_archive.py --web-user segio \
  --config /home/segio/cell000/etc/db.conf
```

출력에서 **`Date-selected approval revision: draft-time-merge-1.`**과 기존 `Date-selected mail revision: file-time-merge-1.`을 확인하세요. 인증·CSRF 검증 뒤 `approval_since`에 서버 기준 Unix 초만 받으며 다른 직원 ID·경로·명령을 받지 않습니다. 음수·0·소수·미래 기준 시각과 결재 미선택 시 기준 전달을 거부합니다. 개인 관계 범위를 유지한 읽기 전용 DB 스냅샷에서 기안일 조건을 적용합니다. 결재/메일 추출 시작 시각은 각각 반환하며 Windows가 요청 기준과 응답 규약·기준 시각을 검증한 뒤 기존 백업에 합칩니다.

메일만 백업할 때 결재 기준을, 결재만 백업할 때 메일 기준을 앞당기지 않습니다. 예전 전체 백업의 서버 요청 시각도 기준으로 사용할 수 있습니다. 예전 메일 전용 백업처럼 결재 기준을 확인할 수 없으면 첫 결재를 전체 조회해 기준을 설정하고 로그에 안내합니다. 원본 DB·메일·설정·인증 방법을 변경하지 않으며 예전 Windows 클라이언트와 SSH 직접 추출의 기본 동작은 호환됩니다.

## v0.6.2 백업 기준 시각 이후 메일 추출

Windows 0.6.2의 추가 백업에는 `file-time-merge-1`이 필요합니다. 아래 결재 전체 조회 설명은 Windows 0.6.2~0.6.6에 해당하며 0.6.7은 위의 기안일 기준을 사용합니다. 이전 `mail_baseline` 해시 비교 요청도 호환되며 SSH 프로그램의 통신 규약·기존 동작은 유지합니다. 두 프로그램의 진행 중인 백업이 끝난 뒤 root SSH에서 설치하세요. 재부팅은 필요하지 않습니다.

```bash
TOOLS_DIR=$(mktemp -d /root/segio-tools.XXXXXX) &&
curl -fL https://raw.githubusercontent.com/soriel1004/segio-web/main/downloads/segio-web-0.6.2.zip \
  -o "$TOOLS_DIR/segio-web-0.6.2.zip" &&
unzip -q "$TOOLS_DIR/segio-web-0.6.2.zip" -d "$TOOLS_DIR" &&
cd "$TOOLS_DIR/segio-web-0.6.2/tools" &&
/usr/bin/python ./check_shared_tools.py &&
/usr/bin/python ./install_live_archive.py --web-user segio \
  --config /home/segio/cell000/etc/db.conf
```

검사·설치 결과에서 `Date-selected mail revision: file-time-merge-1.`을 확인하세요. 새 시작 요청의 `mail_since`는 서버 기준 Unix 초만 받으며 로그인 직원의 기존 메일 경로 안에서 파일의 mtime·ctime을 비교합니다. 클라이언트가 경로·명령·대상 ID를 선택하지 않습니다. 기준보다 오래된 파일은 목록 대조만 하고 본문을 읽거나 복사·해시·압축하지 않습니다. 시각이 같은 파일도 포함하고, 수집 시작 시각을 다음 정상 백업 기준으로 반환합니다. 미래 기준 시각·잘못된 값·기존 해시 목록과의 동시 사용은 거부합니다. 새 ZIP·각 원본 파일 검증과 변경 중 재시도·직원 인증·CSRF·중단 후 삭제 검증은 유지합니다. Windows는 기존 보관함에 병합하며 같은 초의 중복을 제거합니다. 결재는 기존 문서의 상태 변경을 놓치지 않도록 현재 DB를 조회합니다.

## v0.6.0 변경분 백업·중단 후 자동 정리

Windows v0.6.0의 추가·변경분 백업에는 `mail-hash-delta-1`, 중단 확인 후 작업기의 자동 삭제에는 `cancel-discard-1`이 필요합니다. 두 프로그램에서 사용 중인 백업이 끝난 뒤 root SSH에서 설치하세요. 그룹웨어 재부팅은 필요하지 않습니다.

```bash
TOOLS_DIR=$(mktemp -d /root/segio-tools.XXXXXX) &&
curl -fL https://raw.githubusercontent.com/soriel1004/segio-web/main/downloads/segio-web-0.6.0.zip \
  -o "$TOOLS_DIR/segio-web-0.6.0.zip" &&
unzip -q "$TOOLS_DIR/segio-web-0.6.0.zip" -d "$TOOLS_DIR" &&
cd "$TOOLS_DIR/segio-web-0.6.0/tools" &&
/usr/bin/python ./check_shared_tools.py &&
/usr/bin/python ./install_live_archive.py --web-user segio \
  --config /home/segio/cell000/etc/db.conf
```

직원 로그인과 CSRF를 확인한 시작 요청만 제한된 메일 검증 목록을 받습니다. 서버는 로그인 직원의 기존 메일 경로에서 열거한 파일만 비교하며 클라이언트 경로·명령·대상 직원 ID를 받지 않습니다. 파일 전체를 안정적으로 읽고 SHA-256이 일치할 때만 전송에서 생략합니다. 변경·삭제·이름 변경은 원본 재대조 규칙을 그대로 적용하며, 변경 MBOX는 전체 전송합니다. 결재는 현재 DB를 다시 조회합니다. Windows는 자신의 이전 백업에서 생략 파일을 검증해 새 폴더에 복사하며 이전 백업을 덮어쓰지 않습니다.

중단 확인 뒤 cleanup의 `discard=1`은 소유자가 일치하는 해당 실행 중 작업에만 자동 정리 표식을 남깁니다. 작업기는 취소를 처리하고 열린 파일을 닫은 뒤 임시 ZIP·work·작업 폴더를 지우고 소유자 확인용 작은 receipt만 남깁니다. DB 통신 중에는 해당 작업의 psql 자식만 종료·회수합니다. 클라이언트가 연결을 잃어도 서버가 표식을 받은 경우 정리 요청이 유지됩니다. 삭제 응답 미확인은 완료로 표시하지 않습니다. 일반 오류로 보존한 PC 원본과 과거 완료 백업은 이 중단 삭제 대상이 아닙니다. SSH 프로그램의 파일 목록과 기존 wire protocol은 유지합니다.

## 백업 후 서버 임시 ZIP 삭제

`verified-cleanup-1`은 개인 로그인 백업의 삭제 요청에서 실제 ZIP과 작업 폴더 삭제를 확인합니다. 실패하면 `cleanup_failed`를 반환하고 성공으로 응답하지 않습니다. 삭제 확인 응답이 사라져도 같은 직원이 같은 작업을 재요청할 수 있도록 비공개 확인 JSON을 남깁니다. 요청별 직원 인증·시간 제한 CSRF 검증은 유지합니다. 실행 중인 추출은 취소를 요청하며 삭제 완료로 응답하지 않습니다. 다른 직원의 작업과 서버 원본 결재·메일은 정리 요청으로 삭제하지 않습니다.

Windows v0.5.7은 PC의 ZIP·내부 파일 검증 및 원본 보존 직후 새 토큰을 받아 서버 임시 자료를 삭제합니다. 메일 변환이 끝날 때까지 ZIP을 유지하지 않습니다. 삭제 확인 실패는 PC 백업을 유지하면서 경고하고 manifest에 미확인 상태를 기록합니다.

진행 중인 백업이 끝난 뒤 root SSH에서 다음을 실행하고 각 Windows 프로그램도 v0.5.7로 갱신하세요. 검사·설치 결과에서 `Server cleanup revision: verified-cleanup-1.`을 확인합니다. 그룹웨어 재부팅은 필요하지 않습니다.

```bash
TOOLS_DIR=$(mktemp -d /root/segio-tools.XXXXXX) &&
curl -fL https://raw.githubusercontent.com/soriel1004/segio-web/main/downloads/segio-web-0.5.7.zip \
  -o "$TOOLS_DIR/segio-web-0.5.7.zip" &&
unzip -q "$TOOLS_DIR/segio-web-0.5.7.zip" -d "$TOOLS_DIR" &&
cd "$TOOLS_DIR/segio-web-0.5.7/tools" &&
/usr/bin/python ./check_shared_tools.py &&
/usr/bin/python ./install_live_archive.py --web-user segio \
  --config /home/segio/cell000/etc/db.conf
```

이미 남은 종료 작업은 생성 후 24시간이 지나면 다음 백업 시작 시 정리합니다. `ready`는 서버 추출 완료 상태이므로 기존 직원의 PC 다운로드 완료를 증명하지는 않습니다. 실행 중인 다른 작업을 일괄 삭제하지 마세요. 작은 `jobs/.작업번호.cleanup.json`에는 작업·직원·정리 상태만 있고 압축 자료·메일·암호는 없으며, 작업 폴더가 사라진 확인 기록도 같은 방식으로 만료 정리합니다. 시간에 맞춰 실행되는 cron 작업을 설치하는 것은 아닙니다. 공용 도구 갱신은 SSH 프로그램의 직접 전송·임시 작업 정리 규칙을 유지합니다.

## 메일 변경 자동 재시도·진행률

`mail-retry-progress-1`은 개인 로그인 추출 중 메일 변경 시 변경된 파일만 최대 두 번 재복사합니다. 변경 없는 파일은 재사용하고 최종 원본 목록과 파일 정보를 다시 대조합니다. 새 파일·삭제·이름 변경도 반영하며, 계속 변경되면 완료로 처리하지 않습니다. 메일 정리·이동을 잠시 중단하거나 사용량이 적은 시간에 다시 시도하세요. 원본 메일·권한·인증 설정은 변경하지 않습니다.

서버 상태 조회에 메일 복사 용량·재시도 횟수·ZIP 생성 용량·해시 검사 용량을 추가했습니다. v0.5.5 Windows 프로그램에서 해당 진행률을 표시합니다. 이전 프로그램의 통신 규약과 SSH 파일 목록은 유지됩니다. 아래 root SSH 명령으로 서버 도구를 갱신하고 Windows 프로그램도 v0.5.5로 실행하세요. 진행 중인 백업이 끝난 뒤 설치합니다. 그룹웨어 재부팅은 필요하지 않습니다.

```bash
TOOLS_DIR=$(mktemp -d /root/segio-tools.XXXXXX) &&
curl -fL https://raw.githubusercontent.com/soriel1004/segio-web/main/downloads/segio-web-0.5.5.zip \
  -o "$TOOLS_DIR/segio-web-0.5.5.zip" &&
unzip -q "$TOOLS_DIR/segio-web-0.5.5.zip" -d "$TOOLS_DIR" &&
cd "$TOOLS_DIR/segio-web-0.5.5/tools" &&
/usr/bin/python ./check_shared_tools.py &&
/usr/bin/python ./install_live_archive.py --web-user segio \
  --config /home/segio/cell000/etc/db.conf
```

검사·설치 결과의 `Mail copy revision: mail-retry-progress-1.`을 확인합니다. `db-native-paths-1` 한글 경로 수정도 포함합니다. 기존 PHP runtime-3과 SSH 전용 프로그램은 계속 사용할 수 있습니다. 이 재복사는 개인 로그인 서버 내 메일 복사에 적용하며 SSH 직접 다운로드의 기존 변경 감지·이어받기 규칙은 유지합니다.

Python 2.6에서 단일 대형 파일을 전송 ZIP으로 만들 때 필요한 ZIP64 로컬 헤더도 보완했습니다. 메일을 메모리에 한꺼번에 올리지 않고 스트리밍 압축하며, 실제 Python 2.6의 4GB 초과 희소 파일을 압축·해제해 모든 바이트를 대조했습니다. 이 검사는 40~50GB 운영 메일함 전체 백업 시험을 대신하지 않습니다.

## 결재 DB 추출 단계의 encoding 오류

Python 2.6의 ASCII 웹 데몬 환경에서 Unicode 한글 경로를 파일 API에 넘기면 `UnicodeEncodeError`가 발생할 수 있습니다. `db-native-paths-1`은 POSIX 파일명을 명시적인 바이트 경로로 처리하며 UTF-8·CP949 이름을 확인합니다. 셀 데이터 경로 밖의 파일은 거부하고, 동일한 표시 이름에 서로 다른 파일이 있으면 임의 선택하지 않습니다. 원본 첨부 바이트·DB 텍스트·인증 설정은 변경하지 않습니다.

개인 로그인 방식은 **서버에 설치된 tools를 갱신**해야 적용됩니다. 다음은 root SSH에서 실행하는 v0.5.3 소스 묶음 설치 예입니다.

```bash
TOOLS_DIR=$(mktemp -d /root/segio-tools.XXXXXX) &&
curl -fL https://raw.githubusercontent.com/soriel1004/segio-web/main/downloads/segio-web-0.5.3.zip \
  -o "$TOOLS_DIR/segio-web-0.5.3.zip" &&
unzip -q "$TOOLS_DIR/segio-web-0.5.3.zip" -d "$TOOLS_DIR" &&
cd "$TOOLS_DIR/segio-web-0.5.3/tools" &&
/usr/bin/python ./check_shared_tools.py &&
/usr/bin/python ./install_live_archive.py --web-user segio \
  --config /home/segio/cell000/etc/db.conf
```

검사·설치 결과에 `Approval exporter revision: db-native-paths-1.`이 표시되는지 확인하고 **Windows에서 수집을 다시 실행**하세요. 그룹웨어 재부팅은 필요하지 않습니다. 기존 v0.5.2 Windows 프로그램도 수정된 서버 도구를 사용할 수 있습니다. 같은 오류가 반복되면 새로 생성된 `jobs/last-failure.json`을 확인합니다. 인코딩 오류는 문자셋·예외 종류·확인된 함수명·줄 번호만 추가로 기록하며 문서 내용·파일명·암호·예외 원문은 기록하지 않습니다.

## Windows 프로그램에 적용

1. 두 프로그램의 수집을 중단하고 창을 닫습니다.
2. 각 프로그램 폴더의 기존 `tools`를 별도 위치에 보관합니다.
3. 공용 ZIP의 `tools` 폴더 전체를 각각 `SEGIOArchive.exe` 옆에 넣습니다. `tools/tools`로 중첩하지 마세요. 소스 실행이라면 각 프로젝트의 최상위 `tools`를 갱신합니다.
4. 두 프로그램에서 각각 실행합니다. SSH 프로그램은 서버 SSH 계정·암호와 대상 SEGIO ID를 사용하고, 개인 로그인 프로그램은 직원 본인의 SEGIO ID·암호를 사용합니다.

파일 몇 개만 섞어 교체하지 마세요. 기존 백업 폴더·원본 자료·프로그램의 `_internal`은 교체하지 않습니다.

## 서버에 적용

**SSH 방식만 사용하면 서버 설치는 필요 없습니다.** Windows의 SSH 프로그램이 공용 `tools` 중 필요한 6개를 매번 비공개 `/tmp/.segio-ssh-*` 작업 폴더로 올립니다.

**개인 로그인 다운로드도 사용하려면** 관리자가 공용 `tools` 전체를 서버의 `/root/segio-shared-tools/tools` 같은 관리용 경로에 복사하고 다음을 실행합니다. 두 프로그램의 다운로드가 없는 시간에 갱신하세요.

```bash
cd /root/segio-shared-tools/tools
python ./check_shared_tools.py
python ./install_live_archive.py --web-user segio \
  --config /home/segio/cell000/etc/db.conf
```

`segio`는 실제 **웹 서비스의 Linux 실행 계정**으로 바꿉니다. 직원 SEGIO ID가 아닙니다. 설치는 root로, 웹 서비스와 추출 작업은 기존 비-root 계정으로 실행합니다. 서버에는 기존 Python 2.6 이상과 psql을 사용합니다.

예전에 `--private-root`를 별도 지정했다면 **같은 경로**를 이번 명령에도 지정하세요. 기존 설치의 사용자 지정 메일 경로, CSRF 비밀값, psql 경로와 `jobs` 내용은 유지합니다. 메일 경로를 바꿀 때만 `--mail-template`를 명시합니다. 설치 중에는 DB 암호·원본 파일 권한·SSH 설정을 바꾸거나 직원 자료를 미리 수집하지 않습니다.

설치기는 두 방식의 공용 추출 코드를 비공개 `etc/segio-live-archive/code`에 넣고, 개인 로그인용 `/segio/archive/live.php`를 갱신합니다. 기존 `bridge.php`는 건드리지 않습니다. SSH 프로그램은 계속 자체 비공개 임시 폴더를 사용하므로 이 설치 경로에 의존하지 않습니다.

공용 파일 누락·Python 구문·JSON·규약 표기를 검사한 뒤 설치 파일을 쓰기 시작합니다. 검사는 로컬 파일 검사이며 서버 인증·DB 접속 성공을 뜻하지 않습니다. 자세한 설치·연결 오류 안내는 [개인 로그인 안내](../docs/WINDOWS_LOGIN.md)를 참고하세요.

## 두 방식 확인

- SSH 프로그램으로 테스트 계정의 자료를 수집하고 완료·SHA-256 검증 결과를 확인합니다.
- 개인 로그인 프로그램으로 같은 테스트 계정의 직원 ID·암호를 입력해 자료를 받은 뒤 본문·첨부를 확인합니다.
- 두 결과는 서로 다른 Windows 저장 폴더에 두는 것이 구분하기 쉽습니다. 원본은 읽기만 하지만 각 방식은 독립된 시점에 추출하므로 다운로드 중 원본이 바뀌면 재시도가 필요합니다.

기존 실행 파일은 재빌드하지 않아도 공용 `tools`를 읽습니다. 이번 공용 묶음 검증은 합성 자료로 수행하며, 운영 서버 인증·접근 권한·실제 자료 누락 여부는 현장에서 확인해야 합니다.

## Python 진단은 정상인데 secure_random_unavailable이 계속될 때

서버의 웹 계정에서 Python을 직접 실행하는 검사와 네이티브 PHP가 Python을 실행하는 검사는 다릅니다. 새 도구의 PHP 연결기는 프로세스 종료 코드를 읽은 뒤 보존하며, 출력을 제한하고 작업기 응답을 최대 15초 기다립니다. 확인된 실패 종료 코드는 계속 거부합니다. 종료 코드가 확인되지 않는 환경에서는 새 작업기의 구조화된 응답(`segio-worker-control/1`)을 검사합니다. 난수는 OS 난수 생성 성공 응답을, 수집 시작은 요청한 작업 번호와 일치하는 접수 응답을 요구합니다. 종료 코드 `-1`은 진단에 그대로 남기며 작업 접수를 수집 완료로 간주하지 않습니다.

서버 설치를 새 묶음으로 갱신한 뒤 수집을 다시 실행하세요. 계속 실패하면 **기존 SEGIO에 로그인한 브라우저**에서 로그인 주소와 같은 서버의 다음 경로를 엽니다.

```text
/segio/archive/live.php?action=diagnostic
```

예를 들어 사내 주소는 `http://192.168.0.100/segio/archive/live.php?action=diagnostic`입니다. 이 응답의 `bridge_revision`은 `php-runtime-3`여야 합니다. `unknown_action`이면 이전 PHP 연결기가 서비스 중이거나 갱신한 서버와 접속한 서버가 다른지 확인합니다.

진단 응답은 함수 사용 가능 여부·접근 가능 여부·고정 상태·종료 코드·출력 길이만 담습니다. 암호·쿠키·작업 번호·설정 경로·작업기의 원문 출력은 반환하지 않으며 DB 조회나 직원 자료 수집을 시작하지 않습니다. 로그인하지 않은 요청은 거부합니다.

`worker.state`가 `ok`이면 그 PHP 요청에서 Python 작업 번호 생성은 정상입니다. `process_unavailable`은 필요한 PHP 함수 제한, `spawn_failed`는 프로세스 생성 실패, `worker_exit_failed`는 Python 자식 프로세스 실패, `exit_status_unavailable`은 종료 상태와 유효한 접수 응답을 확인하지 못함, `acknowledged_exit_unavailable`은 종료 상태는 없지만 작업기 응답을 확인함, `timeout`은 응답 시간 초과를 뜻합니다. 해당 JSON을 기준으로 웹 서비스 설정과 Python 실행 환경을 추가 진단합니다.

### shttpd에서 exit_code=-1, output_bytes=33인 경우

직접 Python 실행은 정상이고 PHP에서 종료 코드를 잃는 경우가 확인되었습니다. PHP와 Python 작업기를 **같은 새 공용 묶음**으로 설치해야 합니다. 작업기는 `--bridge-control`을 사용할 때만 구조화된 성공·접수 응답을 내보내며 기존 `--random-token` 단독 호출은 그대로 32자리 난수를 출력합니다. SSH 업로드 파일 목록과 기존 SSH 프로토콜도 유지합니다.

새 PHP 파일을 서비스에 반영하려면 업무 접속에 영향을 주지 않는 시간에 `/etc/init.d/shttpd restart`를 실행하고 프로그램을 다시 시작합니다. 접속 중인 사용자의 연결은 잠시 끊길 수 있습니다. 이 재시작은 설치 명령이 자동으로 수행하지 않습니다. 수집 완료는 기존 준비 상태·다운로드·해시 검증으로 별도 확인합니다.
