# Argo CD core 설정 반영

`argocd-cm`(URL, 로컬 계정, custom health 규칙)과 `argocd-rbac-cm`은 Ansible이 설치하는 Argo CD
Helm release만 소유한다. 원본은 [argo-cd-values.yaml](../../ansible/roles/argocd/files/argo-cd-values.yaml)이다.
`manifests/`·`argocd/`·`bootstrap/`에 `argocd-cm`을 선언하면 `scripts/validate_argocd.py`가 실패한다.
root Application은 이 release를 관리하지 않으므로 values 변경은 Git merge만으로 서버에 반영되지 않는다.

## 사용할 playbook

- bootstrap을 마친 서버의 core values 변경(계정·RBAC·health 규칙·controller metrics·URL)은
  [`argocd.yml`](../../ansible/playbooks/argocd.yml)로 반영한다. 이 playbook은 `argocd` role만 실행한다.
  SSH·UFW·K3s 설정·ESO 자격증명과 root Application을 건드리지 않고 ESO key도 요구하지 않는다.
- 빈 서버 최초 설치, root Application 생성·단계 전환, K3s 버전 변경처럼 `argocd` role 밖의 변경은
  [서버 초기 구성](ansible-bootstrap.md)의 `bootstrap.yml`을 따른다. `argocd.yml`은 K3s·kubeconfig·Helm이나
  `deployed` 상태의 `argocd` release가 없는 서버, 서버의 K3s 버전이 Git과 다른 서버에서 변경 전에 중단한다.

## values 변경 반영

1. PR 검토 때 고정 chart로 렌더한 manifest 차이를 본다. `validate.sh`는 이 렌더를 `argocd.yaml`로 남긴다.
   4번 미리보기는 Helm values만 보여주므로 렌더 결과의 검토는 이 단계에서 끝낸다.

   ```bash
   git switch --detach origin/main
   VALIDATION_OUTPUT_DIR=/tmp/argocd-render-main ./scripts/validate.sh
   git switch --detach <PR head>
   VALIDATION_OUTPUT_DIR=/tmp/argocd-render-pr ./scripts/validate.sh
   diff -u /tmp/argocd-render-main/argocd.yaml /tmp/argocd-render-pr/argocd.yaml
   ```

2. 변경 PR이 `main`에 merge되고 `Static validation`이 통과했는지 확인한다.
3. 관리자 PC의 저장소를 `main`으로 맞추고 `ansible/`에서 문법 검사와 미리보기를 실행한다.

   ```bash
   git switch main && git pull --ff-only
   cd ansible
   source .venv/bin/activate
   ansible-galaxy collection install -r collections/requirements.yml
   ansible -i inventories/idc-new/hosts.yml k3s_servers --list-hosts
   ansible-playbook -i inventories/idc-new/hosts.yml --syntax-check playbooks/argocd.yml
   ansible-playbook -i inventories/idc-new/hosts.yml --check --diff --tags preflight playbooks/argocd.yml
   ```

   playbook은 먼저 `origin/main`을 fetch한다. checkout에 수정·미추적 파일이 있거나 `HEAD`가 `origin/main`과
   다르면 서버에 접속하기 전에 중단한다. root Application이 core release를 self-heal하지 않으므로 검토되지 않은
   values가 live release에 남지 않게 하기 위해서다. 이 검사를 끄는 변수는 없다. 긴급 변경도 revert나 수정 PR을
   `main`에 merge한 뒤 실행한다.
4. 미리보기 출력을 확인한다. `--check --diff --tags preflight`는 서버를 바꾸지 않는다. checkout·inventory를
   검사하고 서버에서 bootstrap 완료 여부, root 소유·`0600` kubeconfig, Git과 같은 K3s 버전, `deployed`
   release를 확인한다. 그다음 `helm get values argocd`로 읽은 live values와 Git values의 차이, live와 Git의
   chart 버전을 출력한다. `kubernetes.core.helm`이 upgrade 여부를 정하는 비교와 같은 기준이므로 이 차이가
   의도한 변경뿐인지 확인한다.

   playbook은 `origin/main`의 `argocd` role 전체를 적용한다. merge만 하고 반영하지 않은 이전 core 변경이 있으면
   함께 들어간다. AppProject·namespace PSA label처럼 role이 `kubectl`로 적용하는 변경은 이 미리보기에 나오지 않는다.

   태그 없는 `--check`는 playbook이 바로 중단한다. role의 `kubectl diff`·`helm version` 같은 command 작업이
   check mode에서 실행되지 않아 차이가 없는 것처럼 보이고 Helm 버전 검사에서 실패하기 때문이다.
   `--skip-tags preflight`는 위 안전 검사를 모두 건너뛰므로 쓰지 않는다.
5. 같은 checkout에서 실제로 실행한다.

   ```bash
   ansible-playbook -i inventories/idc-new/hosts.yml playbooks/argocd.yml
   ```

   values나 chart가 바뀐 경우에만 Helm upgrade를 수행하고, 준비 대기에 실패하면 `atomic`으로 이전 revision에
   되돌린다. values가 바뀌면 chart의 `checksum/cm` annotation 때문에 argocd-server·repo-server·
   application-controller Pod가 재시작해 UI·sync가 잠시 멈출 수 있다. 이미 배포된 앱 workload는 재시작하지 않는다.
6. 반영 결과를 확인한다. 계정·RBAC를 바꿨다면 admin 로그인, viewer 조회 성공, 변경 권한 거부를 확인한다.
   health 규칙을 바꿨거나 기존 health gate가 남았는지 볼 때는 개인 관리자 SSH 세션에서 다음을 실행한다.
   ConfigMap 값은 출력하지 않는다.

   ```bash
   sudo -n k3s kubectl -n argocd get configmap argocd-cm \
     -o go-template='{{range $k, $v := .data}}{{$k}}{{"\n"}}{{end}}' | grep resource.customizations.health
   sudo -n k3s kubectl -n argocd get application cluster root \
     -o custom-columns=NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status
   ```

   values의 health 키가 모두 보이고 `cluster`와 `root`가 `Synced`·`Healthy`면 반영이 끝난다.

## 기존 클러스터 전환 (2026-11-04 완료)

health 규칙을 `manifests/cluster/argocd-health.yaml`에서 Helm values로 옮긴
[#41](https://github.com/UMC-PRODUCT/umc-product-infra/pull/41)의 1회 전환은 운영 클러스터에서 2026-11-04에 끝났다.
`bootstrap.yml`로 새로 설치한 서버는 처음부터 Helm release가 `argocd-cm`을 소유하므로 이 전환이 필요 없다.
#41 이전 상태의 클러스터를 다시 다룰 때만
[전환 당시 절차](https://github.com/UMC-PRODUCT/umc-product-infra/blob/8e62f4f8bad8a6f0157e029b94eb926be270d682/docs/operations/argocd-config.md#기존-클러스터-전환-1회)를 따른다.
