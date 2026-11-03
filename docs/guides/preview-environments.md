# PR Preview 사용 가이드

Preview는 PR의 변경 사항을 dev에 합치기 전에 테스트하는 임시 환경이다.
인프라 담당자가 Preview 활성화를 완료한 뒤 아래 순서로 사용한다.

## 사용 조건

- `umc-product-server` 저장소 내부 브랜치에서 `develop`으로 보내는 PR이어야 한다.
- 관리자가 작성자와 코드를 확인한 뒤 `preview` 라벨을 붙인다. fork·외부 PR은 사용하지 않는다.
- 동시에 사용할 수 있는 Preview는 최대 2개다. 이미 2개가 있으면 라벨이 자동으로 떼어지고
  사용 중인 PR이 코멘트로 안내된다. 자리가 나면 라벨을 다시 붙인다.

## 1. Preview 켜기

1. 테스트할 PR을 열고 관리자에게 `preview` 라벨 추가를 요청한다.
2. 라벨이 붙으면 PR에 자리 배정 코멘트가 달린다. 다음 경우에는 라벨이 자동으로 떼어지고 이유가 코멘트로 남는다.
   - 이미 Preview 2개가 사용 중일 때
   - `develop`에 있는 마이그레이션이 PR에 없을 때 → `develop`을 merge한 뒤 라벨을 다시 붙인다.
   - PR의 새 마이그레이션 버전이 `develop` 최신보다 낮을 때 → 버전을 바꾼 뒤 라벨을 다시 붙인다.

   이 검사는 라벨을 붙일 때만 한다. 라벨이 붙은 PR에 새 커밋을 올릴 때는 다시 검사하지 않는다.
3. GitHub Actions에서 해당 PR 이미지의 빌드·발행 결과를 확인한다.
4. Argo CD에서 `umc-product-preview-<PR번호>`가 `Synced`·`Healthy`인지 확인한다.
   조회 권한이 없으면 관리자에게 배포 완료 확인을 요청한다.

라벨을 붙인 직후에는 접속되지 않을 수 있다. 빌드와 배포가 끝난 뒤 테스트하며,
새 커밋을 올렸을 때도 **실제 배포된 커밋이 테스트하려는 커밋인지** 확인한다.

실행 이미지는 PR별 마지막 발행 성공 기록의 digest로 고정된다. 최초 성공 기록이 없으면
앱·DB를 만들지 않고, 후속 빌드·테스트·발행이나 infra 갱신이 실패하면 이전 성공 이미지를 유지한다.
Argo CD의 `preview-head-sha`는 최신 PR 커밋, `preview-deployed-sha`는 배포 대상 성공 커밋이다.

## 2. 접속과 테스트

- API 주소: `https://api-pr-<PR번호>.university.neordinary.com`
- API 문서: 위 주소의 `/docs/scalar.html`
- 메일 수집함: `https://mail-pr-<PR번호>.university.neordinary.com`
- 테스트용 토큰: 위 주소의 `/test/token/access`에서 회원 ID로 액세스 토큰을 받는다. dev와 같은 test·seed API가 열려 있다.
- 로그·메트릭·트레이스: Grafana에서 서비스 `preview-umc-product-pr<PR번호>` 선택
  로그는 WARN 이상만, 요청(trace) 10개 중 약 1개만 Loki에 남는다. 요청별 원인은 Tempo trace에서 본다.
  로그로 요청 수를 세는 API 사용량 패널은 실제보다 적게 나오므로 요청 수는 trace나 metric으로 본다.

예를 들어 PR 27의 API 주소는 `https://api-pr-27.university.neordinary.com`이다.
앱에서 테스트할 때는 API 서버 주소를 해당 Preview 주소로 바꾼다.
`/docs`, `/docs-json`, `/test`는 고정 계정 `umc-docs`의 Basic Auth로 보호된다.
모든 PR이 Preview 전용 비밀번호를 공유하며 PR 생성·삭제·재배포 때 바뀌지 않는다.
비밀번호는 관리자가 승인된 채널로 별도 전달한다. 일반 API 인증에는 이 문서용 인증을 추가하지 않는다.

테스트 후에는 앱의 API 서버 주소를 원래 환경으로 되돌린다.

## 3. 개인 DB 접속

PR 전용 DB는 배포 과정에서 자동으로 생성된다. 직접 생성할 필요는 없다.

- 처음 만들 때 **dev DB를 그 시점 그대로 복사**한다. 복사가 끝나기 전에는 앱이 뜨지 않으므로 몇 분 걸릴 수 있다.
- 복사 후 로그인 세션·SSO 인가 코드·이메일 인증 토큰·대기 중인 outbox·Apple refresh token·데모데이 스탬프 QR 자격은 지운다.
  dev 로그인 상태는 이어지지 않으므로 `/test/token/access`로 새 토큰을 받는다.
- 회원·업무 데이터와 Flyway 이력은 그대로다. dev에서 쓰던 파일(S3)은 복사하지 않으므로 기존 이미지·첨부 URL은 열리지 않는다.
  파일이 필요한 기능은 새로 업로드해서 테스트한다. Preview에 올린 파일은 30일 뒤 자동으로 지워진다.
- 같은 PR에 새 커밋을 배포해도 DB는 유지되며, PR의 새 마이그레이션만 적용된다.
- 최신 dev 데이터가 필요하면 `preview` 라벨을 뗐다가 다시 붙인다. 기존 PR DB의 데이터는 모두 버려진다.

DataGrip에서 [SSH 터널 설정](db-access.md#2-datagrip-설정)을 사용하고 다음 정보를 입력한다.

- Host·Port: 관리자가 전달한 Preview DB 접속 정보
- Database: `umc_product_pr<PR번호>` — PR 27이면 `umc_product_pr27`
- User·Password: 관리자가 발급한 개인 PostgreSQL 계정

SSH 계정과 DB 계정은 별개다. Preview DB 접근 권한을 별도로 요청하며, dev/prod 계정이나
앱 공용 계정을 대신 사용하지 않는다. PR별 DB는 나뉘지만 Preview끼리는 기반과 앱 권한을
공유하므로 운영 데이터·개인정보·실제 비밀값을 테스트 데이터로 넣지 않는다.

## 4. Preview 끄기

PR을 닫거나 `preview` 라벨을 제거하면 Preview와 전용 DB가 자동으로 정리된다.

> [!WARNING]
> 해당 DB의 DataGrip 연결도 강제로 끊기며 **DB 데이터가 함께 삭제된다.**
> 필요한 테스트 결과는 미리 저장한다. PR을 다시 열어도 삭제된 데이터는 돌아오지 않는다.

라벨 제거·PR 종료 때 backend의 `preview-release-cleanup.yml`이 성공 기록 파일도 지운다.
그래서 라벨을 다시 붙이면 이전 이미지로 바로 뜨지 않고, 자리·마이그레이션 검사와 이미지 발행을 다시 거친 뒤
새 DB로 생성된다. 이 방법을 DB 초기화에 사용한다.
열린 PR의 성공 기록 파일을 손으로 삭제하면 앱과 DB가 삭제되므로, 파일 삭제를 일시 정지 수단으로 사용하지 않는다.

정리가 끝나지 않으면 직접 DB를 삭제하지 말고 인프라 담당자에게 PR 번호를 전달한다.

## 테스트할 때 알아둘 점

- **이메일:** Preview는 메일을 실제 수신자에게 보내지 않는다. 앱이 보낸 메일은 같은 Pod의 Mailpit에 모이고,
  `https://mail-pr-<PR번호>.university.neordinary.com`에서 원래 수신자·제목·본문을 확인한다.
  문서·test API와 같은 Preview 공용 Basic Auth를 쓰며, PR마다 수신함이 따로다. Pod가 다시 뜨면 수신함은 비워진다.
  실제 메일 서비스에서의 도착·스팸 판정은 dev에서 확인한다.
- **소셜 로그인:** 모바일 앱이 받은 Google·Kakao·Apple 토큰을 Preview API로 보내는 흐름을 테스트한다.
  웹 OAuth의 PR별 동적 콜백은 지원하지 않는다.
- **QR:** QR 웹 주소는 dev와 같은 `https://university.neordinary.com`이다. 별도 웹 Preview를 만들거나
  기존 웹의 API 목적지를 Preview로 자동 전환하지 않는다.
- **배포 실패:** 이전 성공 이미지 유지는 빌드·발행·infra 갱신 실패에 대한 보장이다. 성공 이미지 교체 중에는
  단일 replica `Recreate`로 중단이 생길 수 있고, 앱 기동·Flyway 실패 시 이전 Pod 유지는 보장하지 않는다.
- **데이터 복구:** 앱 버전을 되돌려도 DB 변경은 자동으로 되돌아가지 않는다.

## 문제가 생기면

PR 번호, 발생 시각, 테스트한 커밋, 요청 경로와 가능하면 Trace ID를 함께 전달한다.
로그 확인 방법은 [상황별 대시보드 안내](monitoring.md#어떤-상황에-어떤-대시보드를-볼까)를 참고한다.

인프라 담당자의 활성화 준비와 생성·삭제 실패 점검은 [Preview 운영 절차](../runbooks/preview-operations.md)에 있다.

> [!IMPORTANT]
> 운영자는 Matrix 방식으로 전환하기 전에 기존 Preview 실행 이미지의 검증된 SHA/digest 성공 기록을
> 먼저 준비한다. 기록이 없는 기존 앱·DB는 삭제 대상이 된다. 백엔드 기본 브랜치 `main`에도
> `publish-preview.yml`이 먼저 반영되어 있어야 한다.
> 문서 인증은 `/umc-product/preview/docs-basic-auth` source와 ESO IAM 읽기 권한을 먼저 준비하고,
> chart 반영 후 `preview/docs-basic-auth` ExternalSecret의 `Ready=True`를 확인한다.
