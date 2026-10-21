# 두 프로그램 공용 tools

**[공용 tools ZIP 바로 다운로드](https://github.com/soriel1004/segio/raw/refs/heads/main/downloads/segio-shared-tools.zip)**

이 폴더 전체를 **segio의 SSH 백업 프로그램(v0.4.3)**과 **segio-web의 개인 SEGIO 로그인 프로그램**에 함께 사용할 수 있습니다. SSH 업로드 파일 이름과 두 통신 규약(`segio-ssh-archive/1`, `segio-live-archive/1`)은 유지합니다. 프로그램 본체와 `_internal` 폴더는 각 프로그램의 것을 사용하세요.

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
