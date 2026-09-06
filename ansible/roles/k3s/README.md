# `k3s` 역할

한 대의 IDC 호스트에 고정 버전의 K3s 서버를 설치하고, 클러스터가 다음 역할을 받을 준비가 되었는지 검증한다.

## 하는 일

1. `/etc/rancher/k3s/config.yaml`을 생성한다.
2. checksum으로 검증한 공식 설치 스크립트로 지정 버전의 K3s를 설치하거나 갱신한다.
3. `k3s` 서비스를 활성화하고 Kubernetes API와 단일 노드의 `Ready` 상태를 기다린다.
4. 번들 Traefik이 외부에서 임의로 보낸 forwarded header를 신뢰하지 않도록 설정한다.
5. Kubernetes Secret의 저장 시 암호화가 활성화되었는지 확인한다.
6. 관리자 kubeconfig가 `root:root`, `0600`인지 확인한다.

## 주요 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `k3s_version` | `v1.36.3+k3s1` | 설치할 K3s 버전 |
| `k3s_kubeconfig_path` | `/etc/rancher/k3s/k3s.yaml` | 관리자 kubeconfig 경로 |
| `k3s_cluster_cidr` | `10.42.0.0/16` | Pod 네트워크 |
| `k3s_service_cidr` | `10.43.0.0/16` | Service 네트워크 |
| `k3s_system_reserved_cpu` | `1000m` | 호스트 시스템용 예약 CPU |
| `k3s_system_reserved_memory` | `2Gi` | 호스트 시스템용 예약 메모리 |
| `k3s_ready_timeout` | `300s` | Node와 Traefik 준비 대기 시간 |

디스크·메모리 부족 시 Pod를 축출하는 `k3s_eviction_*` 변수도 `defaults/main.yml`에서 관리한다.

## 파일 구성

| 경로 | 역할 |
|---|---|
| `defaults/main.yml` | 버전, 네트워크, 예약 자원 등의 기본값 |
| `tasks/main.yml` | K3s 설치, 준비 상태 확인, Traefik 설정 |
| `handlers/main.yml` | 설정 변경 시 K3s 재시작 |
| `templates/config.yaml.j2` | K3s 서버 설정 |
| `templates/traefik-config.yaml.j2` | 번들 Traefik의 `HelmChartConfig` |

## 주의사항

- 현재 구현은 **단일 노드 K3s server** 전용이다. 다중 server/agent 구성과 HA join은 지원하지 않는다.
- `common` 역할이 먼저 실행되어 커널과 방화벽이 준비되어야 한다.
- Pod/Service CIDR은 IDC, 호스트, VPN 네트워크와 겹치면 안 되며 기존 클러스터에서 가볍게 바꿀 수 있는 값이 아니다.
- K3s 버전을 바꿀 때 설치 경로와 백업 가능성을 검토하고 같은 release tag의 설치 스크립트 URL과 checksum도 함께 갱신해야 한다.
