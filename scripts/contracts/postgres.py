"""PostgreSQL environment, role, and backup contracts."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from contracts import common
from contracts.common import require, resource, yaml_documents


POSTGIS_IMAGE = (
    "postgis/postgis:18-3.6@sha256:"
    "60f6ad1d21ea86a67d47780b9a0d1e1d200500f62b19293fa834d0dea80b8677"
)
POSTGRES_EXPORTER_IMAGE = (
    "quay.io/prometheuscommunity/postgres-exporter:v0.20.1@sha256:"
    "ac5ec343104fae0e2d84a27bb8d69b38430a11910c5382cad85d478d2bab713e"
)


# DB 환경 분리와 역할·백업·저장 경로를 고정해 chart나 초기화 Job 변경의 데이터 위험을 검사한다.
def validate_postgres() -> None:
    expected_databases = {
        "prod": "umc_product",
        "dev": "umc_product_dev",
        "preview": "preview_bootstrap",
    }
    for environment, expected_database in expected_databases.items():
        directory = common.ROOT / "manifests" / "postgres" / environment
        statefulset = resource(yaml_documents(directory / "statefulset.yaml"), "StatefulSet")
        container = statefulset["spec"]["template"]["spec"]["containers"][0]
        require(container["image"] == POSTGIS_IMAGE, f"{environment}: PostGIS image")
        mounts = {item["name"]: item["mountPath"] for item in container["volumeMounts"]}
        require(mounts["data"] == "/var/lib/postgresql", f"{environment}: PG18 volume path")
        environment_values = {
            item["name"]: item.get("value") for item in container.get("env", [])
        }
        require("PGDATA" not in environment_values, f"{environment}: use image PG18 PGDATA")
        require(
            environment_values["POSTGRES_DB"] == expected_database,
            f"{environment}: database name",
        )
        # 새 데이터 디렉터리는 한글 정렬을 위해 ICU ko-KR로 만든다. libc locale도 image LANG에 기대지 않는다.
        require(
            environment_values.get("POSTGRES_INITDB_ARGS")
            == "--encoding=UTF8 --locale=C.UTF-8 --locale-provider=icu --icu-locale=ko-KR",
            f"{environment}: initdb must use the ICU ko-KR default collation",
        )
        require(
            statefulset["spec"].get("persistentVolumeClaimRetentionPolicy")
            == {"whenDeleted": "Retain", "whenScaled": "Retain"}
            and statefulset["spec"].get("updateStrategy") == {"type": "RollingUpdate"},
            f"{environment}: StatefulSet must retain data PVCs and roll Pods one at a time",
        )
        if environment == "prod":
            # CPU limit은 checkpoint·vacuum을 throttle한다. memory는 request와 limit을 같게 둔다.
            resources = container["resources"]
            require(
                "cpu" not in resources.get("limits", {})
                and resources["requests"].get("cpu")
                and resources["requests"]["memory"] == resources["limits"]["memory"],
                "prod: PostgreSQL keeps a CPU request without a CPU limit and fixed memory",
            )
        if environment == "preview":
            require(
                "max_connections=40" in container.get("args", []),
                "preview: connection budget must match two slots",
            )
            orphan_check = resource(
                yaml_documents(directory / "orphan-database-check-cronjob.yaml"), "CronJob"
            )["spec"]
            require(
                orphan_check["schedule"] == "0 3 * * *"
                and orphan_check["timeZone"] == "Asia/Seoul",
                "preview: orphan DB check runs daily at 03:00 Asia/Seoul",
            )

        role_job = resource(yaml_documents(directory / "app-role-job.yaml"), "Job")
        role_container = role_job["spec"]["template"]["spec"]["containers"][0]
        require(role_container["image"] == POSTGIS_IMAGE, f"{environment}: role Job image")
        app_password = next(item for item in role_container["env"] if item["name"] == "APP_PASSWORD")
        require(
            app_password["valueFrom"]["secretKeyRef"]
            == {"name": "app-db", "key": "DATABASE_PASSWORD"},
            f"{environment}: app DB Secret",
        )
        if environment in {"prod", "dev"}:
            script = role_container["args"][0]
            require("CREATE EXTENSION IF NOT EXISTS postgis;" in script, f"{environment}: postgis")
            require("CREATE EXTENSION IF NOT EXISTS btree_gist;" in script, f"{environment}: btree_gist")
            # 앱 role이 DB owner이면 DROP DATABASE가 가능하다. owner는 관리자에게 두고 필요한 권한만 준다.
            app_role = "umc_product_app" if environment == "prod" else "umc_product_dev_app"
            require(
                'ALTER DATABASE :"DBNAME" OWNER TO CURRENT_USER;' in script
                and f'ALTER DATABASE :"DBNAME" OWNER TO {app_role}' not in script,
                f"{environment}: database owner must stay the admin role",
            )
            require(
                f'GRANT CONNECT, TEMPORARY, CREATE ON DATABASE :"DBNAME" TO {app_role};' in script,
                f"{environment}: app role needs CREATE for the Flyway public schema bootstrap",
            )

    prod_directory = common.ROOT / "manifests" / "postgres" / "prod"
    readonly_script = resource(
        yaml_documents(prod_directory / "readonly-role-job.yaml"), "Job"
    )["spec"]["template"]["spec"]["containers"][0]["args"][0]
    for statement in (
        "CONNECTION LIMIT 10;",
        "ALTER ROLE umc_product_ro SET statement_timeout = '60s';",
        "ALTER ROLE umc_product_ro SET idle_in_transaction_session_timeout = '5min';",
    ):
        require(statement in readonly_script, f"prod: readonly role limit {statement}")
    exporter_role_script = role_job_script(
        prod_directory / "postgres-exporter-role-job.yaml",
        "EXPORTER_PASSWORD",
        {"name": "postgres-exporter", "key": "POSTGRES_EXPORTER_PASSWORD"},
    )
    require(
        "GRANT pg_monitor TO umc_product_exporter;" in exporter_role_script
        and "NOBYPASSRLS" in exporter_role_script
        and "GRANT SELECT" not in exporter_role_script,
        "prod: exporter role must be monitoring-only",
    )

    exporter_resources = yaml_documents(prod_directory / "postgres-exporter.yaml")
    exporter_config = resource(exporter_resources, "ConfigMap")
    exporter_service_account = resource(exporter_resources, "ServiceAccount")
    exporter_service = resource(exporter_resources, "Service")
    exporter_deployment = resource(exporter_resources, "Deployment")
    exporter_pod = exporter_deployment["spec"]["template"]["spec"]
    exporter_container = exporter_pod["containers"][0]
    exporter_env = {item["name"]: item["value"] for item in exporter_container["env"]}
    require(
        exporter_config.get("data")
        == {"postgres_exporter.yml": "auth_modules: {}\n"},
        "prod: exporter config contains no credentials",
    )
    require(
        exporter_service_account.get("automountServiceAccountToken") is False
        and exporter_pod.get("automountServiceAccountToken") is False,
        "prod: exporter Kubernetes API token disabled",
    )
    require(
        exporter_container["image"] == POSTGRES_EXPORTER_IMAGE,
        "prod: postgres-exporter image",
    )
    require(
        exporter_env
        == {
            "DATA_SOURCE_URI": (
                "postgres.db.svc.cluster.local:5432/umc_product?sslmode=disable"
            ),
            "DATA_SOURCE_USER": "umc_product_exporter",
            "DATA_SOURCE_PASS_FILE": (
                "/var/run/secrets/postgres-exporter/POSTGRES_EXPORTER_PASSWORD"
            ),
            "PG_EXPORTER_COLLECTION_TIMEOUT": "8s",
        },
        "prod: exporter connection contract",
    )
    require(
        exporter_service["spec"]
        == {
            "type": "ClusterIP",
            "selector": {
                "app.kubernetes.io/name": "postgres-exporter",
                "app.kubernetes.io/component": "metrics",
            },
            "ports": [
                {
                    "name": "metrics",
                    "port": 9187,
                    "targetPort": "metrics",
                    "protocol": "TCP",
                }
            ],
        },
        "prod: exporter internal-only Service",
    )
    require(
        exporter_container["resources"]
        == {
            "requests": {"cpu": "10m", "memory": "32Mi"},
            "limits": {"memory": "64Mi"},
        },
        "prod: exporter resource budget",
    )
    require(
        exporter_pod["securityContext"].get("runAsNonRoot") is True
        and exporter_container["securityContext"]
        == {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "prod: exporter restricted security context",
    )

    policies = {
        item["metadata"]["name"]: item
        for item in yaml_documents(prod_directory / "networkpolicy.yaml")
        if item.get("kind") == "NetworkPolicy"
    }
    require(
        policies["postgres-exporter-allow-db-egress"]["spec"]
        == {
            "podSelector": {
                "matchLabels": {
                    "app.kubernetes.io/name": "postgres-exporter",
                    "app.kubernetes.io/component": "metrics",
                }
            },
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [
                        {
                            "podSelector": {
                                "matchLabels": {
                                    "app.kubernetes.io/name": "postgres",
                                    "app.kubernetes.io/component": "database",
                                }
                            }
                        }
                    ],
                    "ports": [{"protocol": "TCP", "port": 5432}],
                }
            ],
        },
        "prod: exporter DB-only egress",
    )
    prometheus_ingress = policies["postgres-exporter-allow-prometheus-ingress"][
        "spec"
    ]
    require(
        prometheus_ingress["policyTypes"] == ["Ingress"]
        and prometheus_ingress["ingress"][0]["ports"]
        == [{"protocol": "TCP", "port": 9187}]
        and prometheus_ingress["ingress"][0]["from"]
        == [
            {
                "namespaceSelector": {
                    "matchLabels": {
                        "kubernetes.io/metadata.name": "monitoring"
                    }
                },
                "podSelector": {
                    "matchLabels": {
                        "app.kubernetes.io/name": "prometheus",
                        "operator.prometheus.io/name": "prometheus-kube-prometheus-prometheus",
                    }
                },
            }
        ],
        "prod: exporter metrics ingress restricted to Prometheus",
    )

    require(not (prod_directory / "kube-state-metrics-role.yaml").exists(),
            "prod: kube-state-metrics RBAC is owned by kube-prometheus-stack")
    validate_backup_role(prod_directory)


# role 생성 Job은 고정한 PostGIS image로 실행하고 그 role 전용 Secret의 password만 받는다.
def role_job_script(path: Path, password_env: str, secret: dict[str, str]) -> str:
    require(path.is_file(), f"prod: {path.name} is missing")
    container = resource(yaml_documents(path), "Job")["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item for item in container["env"]}
    require(container["image"] == POSTGIS_IMAGE, f"prod: {path.name} image")
    require(
        env.get(password_env, {}).get("valueFrom", {}).get("secretKeyRef") == secret,
        f"prod: {path.name} must read {password_env} from Secret {secret['name']}",
    )
    return container["args"][0]


def sync_wave(item: dict) -> int:
    return int(item["metadata"].get("annotations", {}).get("argocd.argoproj.io/sync-wave", "0"))


# backup role Job SQL의 마지막 검사다. SQL 문자열 검사로는 따옴표·대소문자를 바꾼 grant를 다 잡을 수 없으므로
# 실행 시점 catalog 상태로 판정하는 이 검사를 그대로 고정한다.
BACKUP_ROLE_FINAL_STATE_CHECK = """\
DO $role_check$
DECLARE
  backup_role oid := (SELECT oid FROM pg_roles WHERE rolname = 'umc_product_backup');
BEGIN
  IF NOT EXISTS (
    SELECT 1
      FROM pg_roles
     WHERE oid = backup_role
       AND rolcanlogin AND rolinherit AND rolconnlimit = 2
       AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
  ) THEN
    RAISE EXCEPTION 'umc_product_backup has unexpected role attributes';
  END IF;
  IF ARRAY(
    SELECT granted_role.rolname || CASE WHEN membership.admin_option THEN ' WITH ADMIN' ELSE '' END
      FROM pg_auth_members membership
      JOIN pg_roles granted_role ON granted_role.oid = membership.roleid
     WHERE membership.member = backup_role
  ) <> ARRAY['pg_read_all_data'] THEN
    RAISE EXCEPTION 'umc_product_backup must be a member of pg_read_all_data only';
  END IF;
  IF EXISTS (
    SELECT 1
      FROM pg_shdepend dependency
     WHERE dependency.refclassid = 'pg_authid'::regclass
       AND dependency.refobjid = backup_role
       AND NOT (
         dependency.deptype = 'a'
         AND dependency.classid = 'pg_database'::regclass
         AND dependency.objid = (SELECT oid FROM pg_database WHERE datname = current_database())
       )
  ) OR EXISTS (
    SELECT 1
      FROM pg_database db, aclexplode(db.datacl) acl
     WHERE acl.grantee = backup_role
       AND (db.datname <> current_database() OR acl.privilege_type <> 'CONNECT' OR acl.is_grantable)
  ) THEN
    RAISE EXCEPTION 'umc_product_backup has privileges beyond pg_read_all_data and CONNECT';
  END IF;
END
$role_check$;
"""


# 443 송신이 있는 backup Pod의 dump는 관리자 Secret 대신 조회 전용 role만 쓰고 table lock을 무한정 기다리지 않는다.
# 그 role은 한 transaction 안에서 최종 권한 검사를 통과해야만 반영되게 한다.
def validate_backup_role(prod_directory: Path) -> None:
    role_job_path = prod_directory / "postgres-backup-role-job.yaml"
    role_script = role_job_script(
        role_job_path,
        "BACKUP_PASSWORD",
        {"name": "postgres-backup", "key": "POSTGRES_BACKUP_PASSWORD"},
    )
    require(
        "\\getenv backup_pw BACKUP_PASSWORD" in role_script and "--set" not in role_script,
        "prod: backup role password must not appear in psql arguments",
    )
    sql = role_script.split("<<'SQL'\n", 1)[-1]
    require(
        sql.startswith("\\getenv backup_pw BACKUP_PASSWORD\nBEGIN;\n")
        and sql.endswith(BACKUP_ROLE_FINAL_STATE_CHECK + "COMMIT;\nSQL\n"),
        "prod: backup role Job must end its single transaction with the pinned final-state check",
    )
    statements = sql[: -len(BACKUP_ROLE_FINAL_STATE_CHECK + "COMMIT;\nSQL\n")]
    require(
        re.findall(
            r"^\s*(BEGIN|COMMIT|END|ROLLBACK|ABORT|START\s+TRANSACTION)\b",
            statements,
            re.IGNORECASE | re.MULTILINE,
        )
        == ["BEGIN"],
        "prod: backup role changes must not commit before the final-state check",
    )
    grants = [
        " ".join(grant.split())
        for grant in re.findall(
            r"\bGRANT\s+([^;]+?)\s+TO\s+\"?umc_product_backup\"?", statements, re.IGNORECASE
        )
    ]
    require(
        grants == ['CONNECT ON DATABASE :"DBNAME"', "pg_read_all_data"],
        f"prod: backup role must be read-only with pg_read_all_data as its only membership: {grants}",
    )

    exporter_path = prod_directory / "postgres-exporter.yaml"
    require(exporter_path.is_file(), "prod: postgres-exporter.yaml is missing; backup role wave is compared with it")
    exporter_wave = max(sync_wave(item) for item in yaml_documents(exporter_path))
    require(
        sync_wave(resource(yaml_documents(role_job_path), "Job")) > exporter_wave,
        "prod: backup role Job must sync after postgres-exporter so its failure does not block the exporter",
    )

    backup_cronjob = resource(yaml_documents(prod_directory / "backup-cronjob.yaml"), "CronJob")
    backup_dump = next(
        (
            item
            for item in backup_cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"].get(
                "initContainers", []
            )
            if item["name"] == "dump"
        ),
        None,
    )
    require(backup_dump is not None, "prod: pg-backup must have an init container named dump")
    assert backup_dump is not None
    require(
        "pg_dump -Fc --lock-wait-timeout=5min " in backup_dump["args"][0],
        "prod: backup pg_dump must fail instead of waiting forever for table locks",
    )
    dump_env = {item["name"]: item for item in backup_dump["env"]}
    require(
        dump_env["PGUSER"].get("value") == "umc_product_backup"
        and dump_env["PGPASSWORD"]["valueFrom"]["secretKeyRef"]
        == {"name": "postgres-backup", "key": "POSTGRES_BACKUP_PASSWORD"}
        and "postgres-secrets" not in yaml.safe_dump(backup_cronjob),
        "prod: pg-backup must dump with the read-only backup role "
        "(umc_product_backup, postgres-backup Secret) and never reference postgres-secrets",
    )

    policies = {
        item["metadata"]["name"]: item["spec"]
        for item in yaml_documents(prod_directory / "networkpolicy.yaml")
        if item.get("kind") == "NetworkPolicy"
    }
    require(
        {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "postgres-backup-role"}}}
        in policies["postgres-allow-required-ingress"]["ingress"][0]["from"]
        and "postgres-backup-role"
        in policies["postgres-bootstrap-allow-db-egress"]["podSelector"]["matchExpressions"][0][
            "values"
        ],
        "prod: backup role Job reaches PostgreSQL 5432 only",
    )
