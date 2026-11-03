# Argo CD core 설정 반영

`argocd-cm`(URL, 로컬 계정, custom health 규칙)과 `argocd-rbac-cm`은 Ansible이 설치하는 Argo CD
Helm release만 소유한다. 원본은 [argo-cd-values.yaml](../../ansible/roles/argocd/files/argo-cd-values.yaml)이다.
`manifests/`·`argocd/`·`bootstrap/`에 `argocd-cm`을 선언하면 `scripts/validate_argocd.py`가 실패한다.
values 변경은 Git merge만으로 반영되지 않는다. 아래 순서로 bootstrap을 다시 실행해야 적용된다.

## values 변경 반영

1. 변경 PR이 `main`에 merge되고 `Static validation`이 통과했는지 확인한다.
2. 관리자 PC에서, 마지막으로 bootstrap을 적용한 revision 이후 `ansible/` 변경이 이번 변경뿐인지 확인한다.
   argocd 역할 밖의 미적용 Ansible 변경이 섞여 있으면 그 변경의 절차와 검증을 먼저 끝내거나 함께 따른다.
   전체 playbook의 check 모드로는 이 범위를 판단할 수 없다(3번 참고). 이 `git log`가 변경 범위의 기준이다.

   ```bash
   git fetch origin
   git log --oneline <마지막 bootstrap 적용 revision>..origin/main -- ansible/
   ```

3. `main`을 checkout한 관리자 PC의 `ansible/`에서 문법 검사와 preflight check만 먼저 실행한다. 재실행에도
   ESO key 환경변수가 필요하다([Secret 운영](secrets.md)).

   ```bash
   git switch main && git pull --ff-only
   cd ansible
   source .venv/bin/activate
   ansible -i inventories/idc-new/hosts.yml k3s_servers --list-hosts
   ansible-playbook -i inventories/idc-new/hosts.yml --syntax-check playbooks/bootstrap.yml
   ansible-playbook -i inventories/idc-new/hosts.yml --check --tags preflight playbooks/bootstrap.yml
   ```

   playbook 전체를 `--check`로 돌리지 않는다. check 모드에서는 `ansible.builtin.command` task가 실행되지 않아
   Helm 버전·K3s secrets-encrypt 상태를 읽는 assert가 빈 결과로 실패한다. 변경 범위는 2번의 `git log`로 판단한다.
4. 같은 inventory로 `ansible-playbook -i inventories/idc-new/hosts.yml playbooks/bootstrap.yml`을 실행한다.
   values가 바뀌면 chart의 `checksum/cm` annotation 때문에 argocd-server·repo-server·application-controller
   Pod가 재시작한다. 앱 workload는 재시작하지 않는다.

## 기존 클러스터 전환 (1회)

`argocd-cm` health 규칙을 `manifests/cluster/argocd-health.yaml`에서 Helm values로 옮긴 PR
([#41](https://github.com/UMC-PRODUCT/umc-product-infra/pull/41))을 운영 클러스터에 반영할 때 한 번 수행한다.
예전 live `argocd-cm`에는 `cluster` Application의 tracking annotation과 예전 field manager
`umc-ansible-argocd-health`가 남아 있다. `kubectl` 명령은 개인 관리자 SSH 세션에서, `git` 명령은 관리자 PC에서 실행한다. ConfigMap 값은 출력하지 않는다.

> [!CAUTION]
> merge 뒤 `cluster` app은 `argocd-cm`을 "prune 필요"로 표시한다. `cluster`는 `prune: false`라 자동 삭제는
> 없지만, 누군가 prune 옵션으로 수동 sync하면 `argocd-cm`이 삭제되고 Argo CD 설정이 사라진다.
> 아래 1번 annotation이 이 경로를 막으므로 merge 전에 반드시 적용한다.

### merge 전

1. live `argocd-cm`에 prune·삭제 보호 annotation을 붙인다. 이미 값이 있으면 실패하므로 그때는 중단하고 기존 값을 확인한다.

   ```bash
   sudo -n k3s kubectl -n argocd annotate configmap argocd-cm \
     argocd.argoproj.io/sync-options=Prune=false,Delete=false
   ```

   Argo CD는 prune 대상의 sync option을 Git이 아니라 live 객체의 annotation에서 읽는다. `Delete=false`는
   `cluster` Application 자체가 finalizer로 삭제될 때도 `argocd-cm`을 남긴다.
   `cluster`는 client-side apply로 selfHeal하므로 이 annotation을 지우지 않는다. diff와 apply 모두
   `last-applied-configuration`에 있던 필드만 비교·삭제 대상으로 보는데, 수동 annotation은 그 기록에 없다.
2. 3분 이상(Argo CD 기본 reconcile 주기) 기다린 뒤 annotation이 남아 있고 `cluster`가 `Synced`인지 확인한다.
   두 조건을 모두 확인한 뒤에만 PR을 merge한다.

   ```bash
   sudo -n k3s kubectl -n argocd get configmap argocd-cm \
     -o jsonpath='{.metadata.annotations.argocd\.argoproj\.io/sync-options}{"\n"}'
   sudo -n k3s kubectl -n argocd get application cluster \
     -o custom-columns=SYNC:.status.sync.status,HEALTH:.status.health.status
   ```

### merge 후

3. `cluster` app이 merge commit을 반영했고 `argocd-cm`을 prune 대상으로 보는지 확인한다.
   `MERGE_SHA`에는 merge commit의 전체 SHA를 넣는다.

   ```bash
   MERGE_SHA='<merge commit 전체 SHA>'
   sudo -n k3s kubectl -n argocd get application cluster \
     -o jsonpath='{.status.sync.revision}{"\n"}{range .status.resources[?(@.name=="argocd-cm")]}{.kind} {.name} requiresPruning={.requiresPruning}{"\n"}{end}'
   ```

   첫 줄이 `MERGE_SHA`와 같고 `ConfigMap argocd-cm requiresPruning=true`가 보일 때만 4번으로 넘어간다.
   그 사이 다른 commit이 `main`에 들어와 첫 줄이 다르면 `git merge-base --is-ancestor "$MERGE_SHA" <첫 줄 SHA>`가
   성공해야 한다. 조건이 맞지 않으면 refresh하고 1분 뒤 다시 확인한다.
   예전 revision 상태에서 4번을 실행하면 selfHeal이 tracking annotation을 다시 붙인다.

   ```bash
   sudo -n k3s kubectl -n argocd annotate application cluster argocd.argoproj.io/refresh=normal --overwrite
   ```

4. GitOps 추적 metadata를 지운다. data는 바뀌지 않는다.

   ```bash
   sudo -n k3s kubectl -n argocd annotate configmap argocd-cm \
     argocd.argoproj.io/tracking-id- kubectl.kubernetes.io/last-applied-configuration-
   ```

5. [values 변경 반영](#values-변경-반영) 순서로 bootstrap을 다시 실행해 Helm이 health 키를 소유하게 한다.
   Ansible 보강 PR([#50](https://github.com/UMC-PRODUCT/umc-product-infra/pull/50))이 먼저 merge됐다면
   그 변경(sshd·UFW·자동 업데이트 등)도 이 실행에서 함께 적용된다. 그 경우 2번의 변경 범위에 #50의
   역할 변경도 들어가므로 #50의 재실행 확인 항목을 함께 따른다.

### 예전 field manager 해제

`umc-ansible-argocd-health`가 health 키를 계속 공동 소유하면, 나중에 values에서 키를 지워도 live 키가
남는다. server-side apply는 다른 manager가 소유한 필드를 지우지 않기 때문이다.

6. manager별로 소유한 health 키를 확인한다. 값은 managedFields에 없으므로 출력되지 않는다.

   ```bash
   sudo -n k3s kubectl -n argocd get configmap argocd-cm --show-managed-fields -o json \
     | python3 -c 'import json,sys; [print(m["manager"], m["operation"], *sorted(k[2:] for k in m.get("fieldsV1",{}).get("f:data",{}) if k.startswith("f:resource.customizations.health."))) for m in json.load(sys.stdin)["metadata"]["managedFields"]]'
   ```

   `helm Apply` 줄에 health 키 6개가 모두 있어야 한다. 없으면 5번이 끝나지 않은 것이므로 해제하지 않는다.
7. data 없는 ConfigMap을 같은 manager로 server-side apply하면 그 manager의 소유만 풀린다. helm이 함께
   소유한 키는 남는다. 먼저 server dry-run으로 사라지는 키가 없는지 비교하고, 차이가 없을 때만 실제로 적용한다.

   ```bash
   CM_KEYS='{{range $k, $v := .data}}{{$k}}{{"\n"}}{{end}}'
   EMPTY_CM='{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"argocd-cm","namespace":"argocd"}}'
   diff \
     <(sudo -n k3s kubectl -n argocd get configmap argocd-cm -o go-template="$CM_KEYS") \
     <(echo "$EMPTY_CM" | sudo -n k3s kubectl apply --server-side --field-manager=umc-ansible-argocd-health \
         --dry-run=server -o go-template="$CM_KEYS" -f -) \
   && echo "$EMPTY_CM" | sudo -n k3s kubectl apply --server-side --field-manager=umc-ansible-argocd-health -f -
   ```

8. 6번 명령을 다시 실행해 `umc-ansible-argocd-health` 줄에 health 키가 없는지 확인한다.
   `helm` 말고 다른 manager(예: `argocd-controller Update`)에 health 키가 남아 있으면 그 키는 values에서 지워도
   live에 남는다. Update manager의 소유는 apply로 풀 수 없으므로 이 상태를 Issue로 기록한다.
   예전 client-side apply가 data를 실제로 바꾼 적이 없다면 이 manager는 annotation만 소유한다(추정, live 미확인).

### 완료 확인

9. 1번 annotation을 지운다. 4번 뒤에는 `argocd-cm`을 추적하는 Application이 없어 prune 대상이 아니다.

   ```bash
   sudo -n k3s kubectl -n argocd annotate configmap argocd-cm argocd.argoproj.io/sync-options-
   ```

10. 결과를 확인한다.

    ```bash
    sudo -n k3s kubectl -n argocd get configmap argocd-cm \
      -o go-template='{{range $k, $v := .data}}{{$k}}{{"\n"}}{{end}}' | grep resource.customizations.health
    sudo -n k3s kubectl -n argocd get application cluster root \
      -o custom-columns=NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status
    ```

    health 키 6개가 보이고 `cluster`와 `root`가 `Synced`·`Healthy`면 전환이 끝난다.
