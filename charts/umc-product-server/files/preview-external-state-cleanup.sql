-- dev에서 복사한 Preview database의 외부 실행 상태를 지운다. 회원·업무 데이터는 유지한다.
-- 테이블 목록은 backend Flyway 마이그레이션 기준이다. 새 상태 테이블이 생기면 여기에 추가한다.
-- dev 스키마가 develop보다 늦을 수 있어, 아직 없는 테이블·컬럼은 건너뛴다.
CREATE FUNCTION pg_temp.clear_state(statement text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  EXECUTE statement;
EXCEPTION
  WHEN undefined_table OR undefined_column THEN
    RAISE NOTICE '건너뜀: %', SQLERRM;
END
$$;

-- 부팅 후 poller·scheduler가 다시 실행할 대기 작업
SELECT pg_temp.clear_state('DELETE FROM public.event_outbox');
SELECT pg_temp.clear_state('DELETE FROM public.fcm_outbox');
SELECT pg_temp.clear_state($sql$
  UPDATE public.fcm_token
     SET is_active = FALSE, deactivated_at = COALESCE(deactivated_at, now())
   WHERE is_active = TRUE
$sql$);

-- dev에서 발급한 로그인·SSO 상태
SELECT pg_temp.clear_state('DELETE FROM public.sso_authorization_code');
SELECT pg_temp.clear_state('DELETE FROM public.sso_social_login_attempt');
SELECT pg_temp.clear_state('DELETE FROM public.refresh_token');

-- 이메일 인증·비밀번호 재설정 토큰
SELECT pg_temp.clear_state('DELETE FROM public.email_verification');

-- dev 자격증명에 묶인 외부 토큰과 dev 키로 만든 QR 자격. 소셜 연결 행 자체는 유지한다.
SELECT pg_temp.clear_state($sql$
  UPDATE public.member_oauth SET apple_refresh_token = NULL WHERE apple_refresh_token IS NOT NULL
$sql$);
SELECT pg_temp.clear_state($sql$
  UPDATE public.demoday_booth
     SET stamp_credential_hash = NULL,
         stamp_credential_cipher = NULL,
         stamp_credential_generated_at = NULL
   WHERE stamp_credential_hash IS NOT NULL
$sql$);
