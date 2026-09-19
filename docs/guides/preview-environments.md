# PR Preview 사용 가이드

Preview는 PR의 변경 사항을 dev에 합치기 전에 테스트하는 임시 환경이다.
인프라 담당자가 Preview 활성화를 완료한 뒤 아래 순서로 사용한다.

## 사용 조건

- `umc-product-server` 저장소 내부 브랜치에서 `develop`으로 보내는 PR이어야 한다.
- 관리자가 작성자와 코드를 확인한 뒤 `preview` 라벨을 붙인다. fork·외부 PR은 사용하지 않는다.
- 동시에 사용할 수 있는 Preview는 최대 3개다. 자리가 없으면 사용이 끝난 PR부터 정리한다.

## 1. Preview 켜기

1. 테스트할 PR을 열고 관리자에게 `preview` 라벨 추가를 요청한다.
2. GitHub Actions에서 해당 PR 이미지의 빌드·발행 결과를 확인한다.
3. Argo CD에서 `umc-product-preview-<PR번호>`가 `Synced`·`Healthy`인지 확인한다.
   조회 권한이 없으면 관리자에게 배포 완료 확인을 요청한다.

라벨을 붙인 직후에는 접속되지 않을 수 있다. 빌드와 배포가 끝난 뒤 테스트하며,
새 커밋을 올렸을 때도 **실제 배포된 커밋이 테스트하려는 커밋인지** 확인한다.

## 2. 접속과 테스트

- API 주소: `https://api-pr-<PR번호>.university.neordinary.com`
- API 문서: 위 주소의 `/docs/scalar.html`
- 로그·메트릭·트레이스: Grafana에서 서비스 `preview-umc-product-pr<PR번호>` 선택

예를 들어 PR 27의 API 주소는 `https://api-pr-27.university.neordinary.com`이다.
앱에서 테스트할 때는 API 서버 주소를 해당 Preview 주소로 바꾼다.
문서 접속에 인증을 요구하면 관리자가 별도로 전달한 문서용 계정을 사용한다.

테스트 후에는 앱의 API 서버 주소를 원래 환경으로 되돌린다.

## 3. 개인 DB 접속

PR 전용 DB는 배포 과정에서 자동으로 생성된다. 직접 생성할 필요는 없다.
같은 PR에 새 커밋을 배포해도 DB는 유지되며, 마이그레이션으로 구조나 데이터가 바뀔 수 있다.

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

정리가 끝나지 않으면 직접 DB를 삭제하지 말고 인프라 담당자에게 PR 번호를 전달한다.

## 테스트할 때 알아둘 점

- **이메일:** Preview는 SES를 사용한다. SES 샌드박스에서는 검증된 수신자만 테스트할 수 있으며,
  dev의 Gmail 발송 성공과는 별개다.
- **소셜 로그인:** 모바일 앱이 받은 Google·Kakao·Apple 토큰을 Preview API로 보내는 흐름을 테스트한다.
  웹 OAuth의 PR별 동적 콜백은 지원하지 않는다.
- **QR:** QR이 여는 기존 웹은 자동으로 Preview API에 연결되지 않는다.
- **데이터 복구:** 앱 버전을 되돌려도 DB 변경은 자동으로 되돌아가지 않는다.

## 문제가 생기면

PR 번호, 발생 시각, 테스트한 커밋, 요청 경로와 가능하면 Trace ID를 함께 전달한다.
로그 확인 방법은 [상황별 대시보드 안내](monitoring.md#어떤-상황에-어떤-대시보드를-볼까)를 참고한다.

인프라 담당자의 활성화 준비와 생성·삭제 실패 점검은 [Preview 운영 절차](../runbooks/preview-operations.md)에 있다.
