"""메일 provider 전환 시 발신자·Secret·네트워크 경계를 검증한다."""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


# 실제 Helm 렌더에서 SMTP → SES 복귀 시 발신자와 egress가 함께 바뀌고 Preview 자격증명은 분리된다.
@unittest.skipUnless(shutil.which("helm"), "helm이 필요합니다")
class EmailTransportTests(unittest.TestCase):
    def render(self, environment: str, *overrides: str) -> subprocess.CompletedProcess:
        command = [
            "helm", "template", "umc-product-server", "charts/umc-product-server",
            "--namespace", "app" if environment == "prod" else "dev-app",
            "-f", f"charts/umc-product-server/values-{environment}.yaml",
        ]
        for override in overrides:
            command.extend(["--set-string", override])
        return subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)

    def documents(self, result: subprocess.CompletedProcess) -> list[dict]:
        self.assertEqual(result.returncode, 0, result.stderr)
        return [item for item in yaml.safe_load_all(result.stdout) if item]

    def check_network(self, documents: list[dict], smtp: bool) -> None:
        policies = [item["spec"] for item in documents if item["kind"] == "NetworkPolicy"]
        self.assertEqual(len(policies), 2)
        allow = next(policy for policy in policies if policy.get("egress"))
        self.assertEqual(allow["policyTypes"], ["Ingress", "Egress"])
        self.assertEqual(allow["ingress"][0]["ports"], [{"protocol": "TCP", "port": 8080}])
        public = next(rule for rule in allow["egress"] if "ipBlock" in rule["to"][0])
        self.assertEqual(public["ports"], [
            {"protocol": "TCP", "port": port} for port in ([443, 587] if smtp else [443])
        ])
        block = public["to"][0]["ipBlock"]
        self.assertEqual(block["cidr"], "0.0.0.0/0")
        for cidr in ("10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "100.64.0.0/10"):
            self.assertIn(cidr, block["except"])

    def test_smtp_uses_secret_and_outbound_only(self) -> None:
        for environment in ("prod", "dev"):
            with self.subTest(environment=environment):
                documents = self.documents(self.render(environment))
                self.check_network(documents, smtp=True)
                deployment = next(item for item in documents if item["kind"] == "Deployment")
                container = deployment["spec"]["template"]["spec"]["containers"][0]
                env = {item["name"]: item["value"] for item in container["env"]}
                self.assertEqual(env["EMAIL_PROVIDER"], "smtp")
                self.assertEqual(env["EMAIL_NO_REPLY_ADDRESS"], env["SMTP_USERNAME"])
                self.assertNotIn("SMTP_PASSWORD", env)
                self.assertIn({"secretRef": {"name": "app-email", "optional": False}}, container["envFrom"])

    def test_ses_rollback_restores_sender_and_closes_smtp_egress(self) -> None:
        for environment in ("prod", "dev"):
            with self.subTest(environment=environment):
                sender = ("no-reply@university.neordinary.com" if environment == "prod"
                          else "no-reply-nonprod@university.neordinary.com")
                documents = self.documents(self.render(
                    environment, "env.EMAIL_PROVIDER=ses", f"env.EMAIL_NO_REPLY_ADDRESS={sender}"
                ))
                self.check_network(documents, smtp=False)

    def test_rejects_wrong_transport_sender_and_plaintext_password(self) -> None:
        for environment in ("prod", "dev"):
            for override in (
                "env.EMAIL_PROVIDER=other", "env.EMAIL_PROVIDER=", "env.SMTP_HOST=untrusted.example.com",
                "env.SMTP_PORT=25", "env.SMTP_USERNAME=another@example.com",
                "env.EMAIL_NO_REPLY_ADDRESS=no-reply@university.neordinary.com",
                "env.SMTP_PASSWORD=synthetic-not-a-real-secret",
            ):
                with self.subTest(environment=environment, override=override):
                    self.assertNotEqual(self.render(environment, override).returncode, 0)

    def test_each_namespace_reads_smtp_password_from_its_own_prefix(self) -> None:
        result = subprocess.run(
            ["helm", "template", "umc-secrets", "charts/umc-secrets"],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        for item in self.documents(result):
            if item["kind"] == "ExternalSecret" and item["metadata"]["name"] == "app-email":
                keys = {datum["secretKey"] for datum in item["spec"]["data"]}
                self.assertIn("SMTP_PASSWORD", keys)

    def render_preview(self, *overrides: str) -> subprocess.CompletedProcess:
        command = [
            "helm", "template", "umc-product-preview-42", "charts/umc-product-server",
            "--namespace", "preview",
            "-f", "charts/umc-product-server/values-preview.yaml",
            "--set", "deployment.enabled=true", "--set", "ingress.enabled=true",
            "--set-string", "image.tag=0123456789ab",
            "--set-string", "image.digest=sha256:" + "a" * 64,
            "--set", "fullnameOverride=umc-product-server-pr-42",
            "--set", "ingress.host=api-pr-42.university.neordinary.com",
            "--set", "ingress.tls[0].hosts[0]=api-pr-42.university.neordinary.com",
            "--set", "mailCapture.uiHost=mail-pr-42.university.neordinary.com",
            "--set", "createDatabase.name=umc_product_pr42",
            "--set", "env.DATABASE_URL=jdbc:postgresql://postgres-preview.preview.svc.cluster.local:5432/umc_product_pr42",
            "--set", "env.SPRING_APPLICATION_NAME=preview-umc-product-pr42",
            "--set", "env.APP_ENVIRONMENT=preview-pr-42",
            "--set", "env.SSO_ISSUER=https://api-pr-42.university.neordinary.com",
            "--set", "env.CERTIFICATE_VERIFICATION_URL_TEMPLATE=https://api-pr-42.university.neordinary.com/api/v1/certificates/verify/{serialNumber}",
            "--set", "env.DEMODAY_QR_BASE_URL=https://university.neordinary.com",
        ]
        for override in overrides:
            command.extend(["--set-string", override])
        return subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)

    def test_preview_mail_stays_in_pod_mailpit_without_relay(self) -> None:
        documents = self.documents(self.render_preview())
        deployment = next(item for item in documents if item["kind"] == "Deployment")
        pod = deployment["spec"]["template"]["spec"]
        self.assertEqual(
            pod["hostAliases"],
            [{"ip": "127.0.0.1", "hostnames": ["smtp-capture.university.neordinary.com"]}],
        )
        app, mailpit = pod["containers"]
        env = {item["name"]: item.get("value") for item in app["env"]}
        self.assertEqual((env["EMAIL_PROVIDER"], env["SMTP_HOST"], env["SMTP_PORT"]),
                         ("smtp", "smtp-capture.university.neordinary.com", "1025"))
        mail_env = {item["name"]: item for item in mailpit["env"]}
        self.assertEqual(mail_env["MP_SMTP_BIND_ADDR"]["value"], "127.0.0.1:1025")
        self.assertEqual(mail_env["MP_SMTP_REQUIRE_STARTTLS"]["value"], "true")
        self.assertEqual(mail_env["SMTP_PASSWORD"]["valueFrom"]["secretKeyRef"],
                         {"name": "app-email", "key": "SMTP_PASSWORD"})
        self.assertFalse([name for name in mail_env if name.startswith("MP_SMTP_RELAY")])
        self.assertIn("@sha256:", mailpit["image"])
        ingress = next(item for item in documents
                       if item["kind"] == "Ingress" and item["metadata"]["name"].endswith("-mail"))
        self.assertEqual(ingress["spec"]["rules"][0]["host"], "mail-pr-42.university.neordinary.com")
        self.assertEqual(ingress["spec"]["tls"][0]["secretName"], "preview-wildcard-tls")
        self.assertIn("-mail-basic-auth@kubernetescrd",
                      ingress["metadata"]["annotations"]["traefik.ingress.kubernetes.io/router.middlewares"])

    def test_preview_rejects_external_smtp(self) -> None:
        for override in (
            "env.SMTP_HOST=smtp.gmail.com", "env.SMTP_PORT=587",
            "env.SMTP_USERNAME=umcproduct1227@gmail.com", "env.EMAIL_PROVIDER=ses",
            "mailCapture.uiHost=mail.example.com",
        ):
            with self.subTest(override=override):
                self.assertNotEqual(self.render_preview(override).returncode, 0)


if __name__ == "__main__":
    unittest.main()
