# UMC Infra 작업 규칙

이 저장소를 수정하는 AI와 자동화 도구는 아래 계약을 먼저 따른다.
진행률이나 외부 서비스의 현재 상태는 문서에서 추측하지 말고 실제 시스템에서 확인한다.

## 관리 경계

- `ansible/`: 빈 Ubuntu 서버를 K3s와 Argo CD까지 bootstrap한다.
- `bootstrap/`, `argocd/`: Argo CD Application과 배포 순서를 선언한다.
- `charts/`, `manifests/`: Kubernetes desired state다. 정상 변경은 Git을 통해 Argo CD가 적용한다.
- `cloud/aws/`: AWS IAM·S3·SES CloudFormation이다.
- `observability/`: 사람이 수정하는 dashboard·alert 원본이다.
- `runbooks/`: 데이터 손실이나 서비스 중단 위험이 있는 운영 절차다.

Ansible은 애플리케이션의 일상 배포 도구가 아니다. 앱 image, Helm values와 Kubernetes manifest는
Git에서 변경하고 Argo CD로 수렴시킨다.

## 변경 원칙

- 기존 worktree 변경을 보존하고 요청과 직접 관련된 파일만 수정한다.
- GitOps가 관리하는 live resource를 수동 `kubectl apply`로 고친 뒤 끝내지 않는다.
- fail-closed 값(`bootstrap-required`, 비활성 앱 Deployment/API Ingress, 정지된 backup)과
  미검증 production Certificate/Ingress를 선행 조건 검증 없이 열지 않는다.
- K3s·Helm·chart 버전을 바꿀 때 연결된 URL·checksum·image digest도 함께 검증한다.
- 실제 Secret 값, access key, private key, token을 Git·로그·명령행 인자에 남기지 않는다.
- `.env.*`는 Git에 넣지 않으며 Kubernetes Secret 값을 조회·출력하지 않는다.
- 서버 관리는 공인 OpenSSH `22/tcp`와 개인별 Linux 계정·공개키로 한다. 비밀번호 인증과
  전환 완료 후 root 직접 SSH 로그인을 금지하며, private key를 서버·팀원에게 공유하지 않는다.
- 인프라 `admin`은 root에 준하는 sudo 권한을 갖는다. 백엔드 `db_tunnel`은 지정 DB 목적지의
  local TCP forwarding만 허용하고 shell·명령 실행·sudo를 차단한다. DB 계정 권한은 별도다.
- 외부에는 SSH `22/tcp`와 HTTPS `443/tcp`만 허용한다. DB `5432/tcp`와 Kubernetes API
  `6443/tcp`는 공개하지 않으며, 제공업체 상위 방화벽은 별도로 검증한다.
- 최초/전환 시 `ansible/playbooks/ssh-access.yml`을 `ssh_access_finalize: false`로 실행해
  기존 관리 경로를 보존한다. 개인 관리자 공인 SSH·sudo와 DB 터널을 확인한 뒤 inventory를
  개인 관리자·공인 IP로 바꾸고 `ssh_access_finalize: true`로 마무리한다. 검증 전에 기존
  접속 경로나 Tailscale을 제거하지 않고 제공업체 console 복구 경로를 확보한다.
- 팀원 제거는 SSH 로그인 차단·기존 세션 종료·계정 제거뿐 아니라 개인 DB role의 새 로그인
  차단과 기존 DB 세션 종료도 포함한다. 단순 공개키 삭제만으로 회수 완료라고 판단하지 않는다.
- cluster 조회는 대상 IDC의 `sudo k3s kubectl`을 사용하고 root 전용 kubeconfig를 복사하지 않는다.
- AWS Route 53·GitHub·실제 클러스터 변경은 사용자가 요청한 범위와 대상 계정을 먼저 확인한다.
- 복구·회전·삭제 절차는 해당 runbook의 중단 조건을 생략하지 않는다.

## 검증

저장소 변경 후 루트에서 가능한 검사를 실행한다.

```bash
./scripts/validate.sh
```

도구가 없어 건너뛴 검사가 있으면 성공으로 숨기지 말고 결과에 명시한다. Ansible만 수정했다면
최소한 다음 검사도 수행한다.

```bash
cd ansible
ansible-playbook --syntax-check playbooks/bootstrap.yml
ansible-playbook --syntax-check playbooks/ssh-access.yml
ansible-lint playbooks/bootstrap.yml playbooks/ssh-access.yml
```

실제 inventory나 외부 시스템이 없으면 live 검증을 했다고 주장하지 않는다.

## 작업별 원본

| 작업 | 먼저 볼 파일 |
|---|---|
| 서버 최초 설치·재구성 | `ansible/README.md` |
| Secret 추가·변경·회전 | `docs/guides/secrets.md` |
| DNS·TLS·Route 53 | `docs/guides/domains-tls.md` |
| backup 최초 활성화 | `runbooks/backup-activation.md` |

문서의 고정 계약과 코드가 다르면 코드를 먼저 확인하고, 차이를 사용자에게 알린 뒤 둘을 함께
수정한다. 날짜별 진행률과 일회성 감사 결과는 저장소 문서가 아니라 Issue나 Project에서 관리한다.
