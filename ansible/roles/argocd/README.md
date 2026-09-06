# `argocd` 역할

K3s 위에 Argo CD core를 고정된 Helm 버전과 차트로 설치하고, GitOps를 시작하기 전 기본 정책을 준비한다.

## 하는 일

1. `argocd` namespace를 만들고 Pod Security 경고·감사 정책을 설정한다.
2. checksum으로 검증한 Helm 바이너리와 Argo CD 차트를 내려받는다.
3. 검토된 `argo-cd-values.yaml`로 Helm release를 설치하거나 갱신한다.
4. root Application보다 먼저 AppProject와 custom health 설정을 적용한다.
5. Argo CD의 주요 Deployment와 application controller가 준비될 때까지 기다린다.

`tasks/root_app.yml`은 일반 작업과 분리되어 있다. `external_secrets_bootstrap`이 secret-zero를 만든 뒤 `bootstrap.yml`의 post-task에서 실행되며, root Application이 `Synced`와 `Healthy`가 될 때까지 기다린다.

## 관리 경계

```text
Ansible: Argo CD core 설치 + bootstrap 정책 + root Application 시작
Argo CD: cert-manager, External Secrets, ExternalDNS, 모니터링, 애플리케이션 관리
```

## 주요 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `argocd_version` | `v3.5.0` | Argo CD 애플리케이션 버전 |
| `argocd_namespace` | `argocd` | 저장소 전체에 고정된 설치 namespace |
| `argocd_helm_version` | `v4.2.4` | 설치할 Helm 버전 |
| `argocd_helm_release_name` | `argocd` | 저장소 전체에 고정된 Helm release 이름 |
| `argocd_helm_chart_version` | `10.3.0` | `argo-cd` 차트 버전 |
| `argocd_helm_release_timeout` | `15m0s` | Helm 수렴 대기 시간 |
| `argocd_root_app_ready_timeout` | `30m` | root Application 상태 대기 시간 |

URL, checksum, 다운로드 경로와 Git manifest 경로는 `defaults/main.yml`에 모여 있다.

## 파일 구성

| 경로 | 역할 |
|---|---|
| `defaults/main.yml` | Helm, 차트, manifest 버전과 경로 |
| `tasks/main.yml` | Helm 설치와 Argo CD core/bootstrap 정책 적용 |
| `tasks/root_app.yml` | root Application 적용과 최종 수렴 확인 |
| `files/argo-cd-values.yaml` | 검토된 Helm values와 자원 제한 |

## 주의사항

- 이 역할은 `k3s`와 root 전용 kubeconfig에 의존하며 제어 노드에는 `kubernetes.core` collection이 필요하다.
- namespace와 release 이름은 Git manifest와 rollout 대상 이름에도 쓰이므로 이 역할에서만 override할 수 없다.
- Helm, 차트, Argo CD 버전과 checksum, values는 호환성을 확인해 한 묶음으로 갱신한다.
- 현재 values는 Argo CD ingress를 만들지 않고 `argocd.invalid`를 사용한다. 외부 공개는 별도 TLS·인증·접근 정책을 설계한 뒤 해야 한다.
- 기존 raw manifest 설치를 Helm이 바로 인수하는 마이그레이션은 다루지 않는다. 새 클러스터 bootstrap을 전제로 한다.
