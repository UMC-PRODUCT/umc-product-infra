# Standalone Prometheus에서 Operator로 전환

대상은 현재 단일 노드 SQLite K3s의 `monitoring` namespace다. 전환은 Git의 `prometheus`
Application을 kube-prometheus-stack으로 바꾸고 기존 Grafana·Loki·Tempo·OTel Collector를 연결한다.
앱·DB workload와 해당 PVC는 이 절차의 대상이 아니다.

## 시작 조건과 중단 조건

새 Prometheus·Alertmanager PVC로 시작하므로 이전 metrics와 silence 상태의 연속성은 보장하지
않는다. 모니터링 중단과 이 이력 초기화가 승인된 작업에서만 이 절차를 실행한다. 기존 Grafana·
Loki·Tempo PVC는 재사용한다. 사전 점검에서 대상 cluster·namespace가 다르거나 root disk의 여유
공간이 부족하면 중단한다. `local-path`의 PVC 크기는 disk quota가 아니다.

관리 명령은 개인 관리자 공인 SSH 경유 IDC의 `sudo -n k3s kubectl`을 사용한다. Secret 값과 root kubeconfig를
복사하거나 출력하지 않는다.

## 적용 순서

1. 현재 Git revision, Argo CD sync 상태, monitoring workload·PVC 이름과 target 상태를 기록한다.
   `./scripts/validate.sh`와 Git diff를 확인한다. chart/CRD/image digest, Operator Role, Prometheus
   selector, custom ServiceMonitor와 NetworkPolicy를 함께 검토한다.
2. 기존 root·Prometheus Application의 자동 sync를 잠시 멈춘다. 검증한 변경을 push하고 CI가
   성공한 뒤 기존 Prometheus server와 Alertmanager를 replica 0으로 내린다. 이 시점부터
   알림·수집 공백이 시작된다. node-exporter의 기존 이름은 유지하므로 host port 9100을 쓰는
   두 DaemonSet을 동시에 만들지 않는다.
3. root Application을 sync해 검증된 Git revision으로 Argo CD를 수렴시킨다. monitoring AppProject의 제한된 cluster 종류
   허용과 wave 1 설정, wave 2 chart, wave 3 integration 순서를 지킨다. CRD가 `Established=True`,
   Operator와 새 Prometheus·Alertmanager가 Ready가 되기 전에 완료로 판단하지 않는다.
4. 기존 Grafana datasource와 Collector가 새 `prometheus-kube-prometheus-prometheus:9090`을
   참조하는지 확인한다. Loki ruler의 Alertmanager 주소는
   `prometheus-kube-prometheus-alertmanager:9093`이다. Targets에서 custom exporter, kubelet,
   cAdvisor, CoreDNS와 kube-state-metrics를 확인하고 rule evaluation 오류와 Collector export 오류가
   없는지 확인한다. Operator가 native `AlertmanagerConfig`의 SecretKeySelector를 읽어 설정을
   생성하고 Alertmanager가 Reconciled·Ready가 되는지 확인한다. 설정 Secret의 내용은 출력하지 않는다.
   `UMC Product`와 `Kubernetes`
   folder가 함께 보이며 기본 홈이 System Overview인지 확인한다.
5. Argo CD 자동 sync를 복원하고 Healthy/Synced와 실제 rollout 상태를 다시 확인한다. 이전
   Prometheus PVC는 prune될 수 있으며 이전 Alertmanager PVC가 남으면 새 workload Ready와
   초기화 승인 범위를 확인한 뒤 그 PVC만 정리한다. CRD와 다른 observability PVC는 삭제하지 않는다.

HTTP 점검이 필요하면 IDC에서 아래 Service를 loopback으로 port-forward하고 SSH tunnel로
연결한다. 외부 Prometheus·Alertmanager Ingress나 `--address 0.0.0.0`은 만들지 않는다.

```bash
sudo -n k3s kubectl -n monitoring port-forward service/prometheus-kube-prometheus-prometheus 19090:9090
sudo -n k3s kubectl -n monitoring port-forward service/prometheus-kube-prometheus-alertmanager 19093:9093
```

설치 확인을 위해 Discord 테스트 알림을 보내지는 않는다. 실제 알림 전달 시험은 별도 요청과
운영 일정에 따라 수행한다.

Alertmanager의 `useExistingSecret: true`는 chart 기본 설정 Secret 생성을 끈다. global
`alertmanagerConfiguration`이 native 설정을 참조하므로 수동 Secret 작성이나 webhook 파일
mount가 필요 없다. 정적 검증은 native CRD schema와 SecretKeySelector 계약을 검사하며,
실제 생성된 설정은 위 Operator reconcile·Alertmanager readiness로 확인한다.

## 실패 시 복구

CRD 설치, Operator reconcile, storage mount 또는 OTLP 수신이 실패하면 자동 prune을 멈추고
실패한 resource의 event·controller log를 먼저 확인한다. 이전 Git revision으로 되돌릴 때는
Collector·Grafana·Loki 주소와 NetworkPolicy도 같이 복원한다. 새 Operator workload를 중지한 뒤
이전 standalone workload를 다시 올리며, 초기화·삭제된 이력은 Git revert로 돌아오지 않는다.
CRD 삭제는 그 CRD의 custom resource도 삭제하므로 rollback 수단으로 사용하지 않는다.
