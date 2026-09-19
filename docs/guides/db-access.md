# DataGrip으로 DB 접속하기

백엔드 팀원이 개인 SSH 키와 발급받은 DB 계정으로 PostgreSQL에 접속하는 안내다.
실제 서버 주소·SSH 계정명·DB 접속 주소·DB 계정·비밀번호는 인프라 담당자가 별도로 전달한다.
비밀번호는 승인된 비밀 전달 수단으로 받으며 Git·채팅방에 남기지 않는다.

SSH 계정은 DB까지 연결하는 터널용이고, PostgreSQL 계정은 DB 로그인과 SQL 권한용이다.
SSH 접속 권한만으로 DB 계정이나 SQL 권한이 생기지 않는다.
prod 조회에는 읽기 전용 DB 계정을 사용하며, 쓰기가 필요하면 업무 범위를 설명하고 별도로 요청한다.

## 1. 개인 키 준비와 접속 요청

본인 컴퓨터에서 `~/.ssh/umc_idc_ed25519`와 `~/.ssh/umc_idc_ed25519.pub`가 이미 있는지 확인한다.
이미 있으면 덮어쓰지 않고 기존 키를 사용하거나 새 파일명을 선택한다.
새 키가 필요하면 아래 명령을 실행하고 Passphrase를 설정한다.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/umc_idc_ed25519 -C "본인이메일"
cat ~/.ssh/umc_idc_ed25519.pub
```

인프라 담당자에게 다음 내용을 전달한다.

- 원하는 SSH 계정명: `jimin`처럼 영문 소문자로 시작하는 2~31자. 영문 소문자·숫자·밑줄만 사용한다.
- `.pub` 공개키 전체 한 줄.
- 필요한 환경: dev, prod, Preview. Preview는 PR 번호도 함께 전달한다.
- 접속 목적과 필요한 읽기·쓰기 범위.

개인키(`.pub`가 없는 파일)와 Passphrase는 본인 컴퓨터에만 두며 공유하지 않는다.
새 파일명을 선택했다면 아래 DataGrip 설정에서도 그 개인키 경로를 사용한다.

## 2. DataGrip 설정

PostgreSQL 데이터 소스를 추가하고 인프라 담당자에게 받은 환경별 접속 정보를 입력한다.
SSH 설정과 General 설정은 서로 다른 접속 정보를 사용한다.

| DataGrip 위치 | 입력 |
|---|---|
| SSH/SSL → Use SSH tunnel | 활성화 |
| SSH Host / Port | 별도로 받은 서버 공인 IP / `22` |
| SSH Username / Authentication | 본인 SSH 계정명 / Key pair |
| SSH Private key | 본인 PC의 `~/.ssh/umc_idc_ed25519` (`.pub` 아님) |
| SSH Passphrase | 키를 만들 때 본인이 설정한 비밀번호 |
| General Host / Port | 별도로 받은 해당 환경의 DB 접속 IP / `5432` |
| General User / Password | 별도로 발급받은 해당 환경의 PostgreSQL 계정·비밀번호 |
| General Database | prod `umc_product`, dev `umc_product_dev`, Preview `umc_product_pr<N>` |

General Host에는 서버 공인 IP나 `localhost` 대신 전달받은 DB 접속 IP를 그대로 입력한다.
Preview DB 이름의 `<N>`은 PR 번호로 바꾼다. 예를 들어 PR 42의 DB 이름은 `umc_product_pr42`다.
SSH Passphrase와 DB Password는 별개다.

## 3. 연결 확인과 정상 동작

DataGrip의 `Test Connection`으로 실제 PostgreSQL 연결이 성공하는지 확인한다.
SSH 연결 성공만으로 DB 연결까지 완료됐다고 판단하지 않는다.
접속 후 선택한 DB가 요청한 환경인지 확인하고 발급받은 권한 범위 안에서 사용한다.

터널용 SSH 계정으로 터미널 shell에 접속하거나 서버 명령을 실행할 수 없는 것은 정상이다.
승인받지 않은 DB 목적지로 연결할 수 없으며, 읽기 전용 DB 계정의 쓰기 작업 거부도 정상이다.

Preview DB는 해당 PR의 배포가 DB를 만든 뒤에만 존재한다.
Preview 종료·정리 전에는 DataGrip 연결을 닫고, 보존할 테스트 데이터가 있는지 확인한다.
생성과 삭제 시점은 [Preview 사용 가이드](./preview-environments.md)를 따른다.

## 문제가 생기면

| 증상 | 확인할 내용 |
|---|---|
| SSH 인증 실패 | SSH 계정명과 개인키 경로가 맞는지, `.pub`를 선택하지 않았는지, Passphrase가 맞는지 확인한다. 등록을 요청한 공개키와 같은 키인지 확인한다. |
| SSH 연결 시간 초과 | 별도로 받은 서버 공인 IP와 SSH 포트 `22`를 확인한다. 계속 실패하면 발생 시간과 오류 메시지를 인프라 담당자에게 전달한다. |
| SSH는 연결되지만 터널 연결이 거부됨 | General Host가 승인받은 환경의 DB 접속 IP인지 확인한다. 이전 접속 정보라면 인프라 담당자에게 현재 주소와 접근 허용 여부를 확인한다. |
| DB 비밀번호 인증 실패 | General의 DB 계정·비밀번호와 대상 환경을 확인한다. SSH Passphrase를 DB Password에 넣지 않았는지 확인한다. |
| DB가 없다는 오류 | DB 이름과 Preview PR 번호를 확인한다. Preview 배포가 완료됐는지, 이미 종료·정리된 환경인지 확인한다. |
| SQL 권한 오류 | 발급받은 계정의 읽기·쓰기 범위를 확인한다. 필요한 작업과 대상 환경을 적어 권한을 요청한다. |
| 터미널 SSH 접속 거부 | 터널용 계정의 정상 동작이다. DataGrip의 실제 DB `Test Connection` 결과를 확인한다. |

해결되지 않으면 환경, Preview PR 번호, 발생 시간과 오류 메시지를 전달한다.
공유할 화면·로그에 비밀번호, 개인키, Passphrase가 포함되지 않도록 한다.

다른 팀 안내는 [문서 목차](../README.md)와 [모니터링 사용 가이드](./monitoring.md)에서 확인한다.
