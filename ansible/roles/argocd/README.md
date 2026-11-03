# `argocd` 역할

K3s 위에 Argo CD core를 고정된 Helm 버전과 차트로 설치하고, GitOps를 시작하기 전 기본 정책을 준비한다.

## 하는 일

1. `argocd` namespace를 만들고 Pod Security `restricted` 경고·감사 label을 설정한다. 기준 버전은
   `k3s_version`의 Kubernetes minor(`v1.36.3+k3s1` → `v1.36`)에서 `vars/main.yml`이 한 번만 만든다.
   enforce는 두지 않는다.
2. checksum으로 검증한 Helm 바이너리와 Argo CD 차트를 내려받는다.
3. 검토된 `argo-cd-values.yaml`로 Helm release를 설치하거나 갱신한다. `argocd-cm`의 custom health 설정도 이 values의 `configs.cm`에 있다.
4. root Application보다 먼저 AppProject를 적용한다.
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
| `tasks/installed_preflight.yml` | `argocd.yml` 전용 bootstrap 완료 확인과 values 차이 미리보기 |
| `files/argo-cd-values.yaml` | 검토된 Helm values와 자원 제한 |

## 주의사항

- 이 역할은 `k3s`와 root 전용 kubeconfig에 의존하며 제어 노드에는 `kubernetes.core` collection이 필요하다.
- namespace와 release 이름은 Git manifest와 rollout 대상 이름에도 쓰이므로 이 역할에서만 override할 수 없다.
- Helm, 차트, Argo CD 버전과 checksum, values는 호환성을 확인해 한 묶음으로 갱신한다.
- `argocd-cm`은 이 Helm release만 소유한다. health 규칙을 바꾸면 Git merge만으로 반영되지 않으므로 [Argo CD core 설정 반영](../../../docs/operations/argocd-config.md) 순서로 `bootstrap.yml`을 다시 실행한다. `manifests/`에 `argocd-cm`을 두면 `validate_argocd.py`가 실패한다.
- core values는 Argo CD의 canonical HTTPS URL과 내부 HTTP backend를 설정한다. 공개 Certificate·Ingress·server NetworkPolicy는 별도 `argocd-access` Application이 관리하며, [공개 접속 순서](../../../docs/operations/domains-tls.md#argo-cd-공개-https)에 따라 적용한다.
- 기존 raw manifest 설치를 Helm이 바로 인수하는 마이그레이션은 다루지 않는다. 새 클러스터 bootstrap을 전제로 한다.
