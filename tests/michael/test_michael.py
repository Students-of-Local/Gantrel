"""Behavior gates for the actual Michael runner, using temporary owned fixtures."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from datetime import datetime, timedelta, timezone
import ipaddress
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[2] / "plugins/michael"
spec = importlib.util.spec_from_file_location("michael", PACKAGE / "michael.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Handler(BaseHTTPRequestHandler):
    hits = 0
    redirect = False

    def do_HEAD(self):
        type(self).hits += 1
        self.send_response(302 if self.redirect else 200)
        if self.redirect:
            self.send_header("Location", "https://8.8.8.8/")
        self.end_headers()

    def log_message(self, *args):
        pass


class MichaelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="michael-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "fixture"
        self.repo.mkdir()
        self.state = self.base / "state"
        self.scope = self.base / "scope.json"
        self.scope_data = {"version": 1, "owned_or_authorized": True, "repositories": [str(self.repo)], "services": []}
        self.write_scope()
        self.seed = "ghp_" + secrets.token_hex(20)
        (self.repo / "example.py").write_text("api_key = '" + self.seed + "'\neval(user_input)\nverify=False\n")
        (self.repo / "compose.yml").write_text("services:\n  app:\n    privileged: true\n")
        (self.repo / "network.tf").write_text('cidr_blocks = ["0.0.0.0/0"]\n')
        (self.repo / "package-lock.json").write_text(json.dumps({"packages": {"node_modules/example": {"version": "1.2.3"}}}))
        self.report = self.base / "offline-trivy.json"
        self.report.write_text(json.dumps({"SchemaVersion": 2, "Metadata": {"TrivyVersion": "0.60.0"}, "Results": [{"Target": self.seed, "Vulnerabilities": [{"VulnerabilityID": "CVE-2024-12345", "Severity": "HIGH", "PkgName": "example", "Description": self.seed, "InstalledVersion": self.seed}]}]}))

    def write_scope(self):
        self.scope.write_text(json.dumps(self.scope_data))

    def cli(self, *arguments, expected=0):
        result = subprocess.run([sys.executable, str(PACKAGE / "michael.py"), "--state", str(self.state), "--scope", str(self.scope), *arguments], text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, expected, result.stderr)
        self.assertNotIn(self.seed, result.stdout + result.stderr)
        return result

    def run_repo(self):
        return json.loads(self.cli("assess-repo", str(self.repo), "--trivy-report", str(self.report)).stdout)["run_id"]

    @contextlib.contextmanager
    def server(self, handler=Handler):
        handler.hits = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_safe_lab_pass(self):
        scope, scope_id = m.scope_at(self.scope)
        original_connect = socket.socket.connect
        with self.server() as url:
            scope["services"] = [url]
            allowed_port = int(url.split(":")[2].split("/")[0])
            def connect(sock, address):
                self.assertEqual(address[:2], ("127.0.0.1", allowed_port))
                return original_connect(sock, address)
            with patch.object(socket.socket, "connect", connect), patch.object(socket, "getaddrinfo", wraps=socket.getaddrinfo) as resolver:
                asset, findings, checks, inventory = m.scan_repo(str(self.repo), scope, str(self.report))
                self.assertTrue(inventory)
                rules = {item["rule"] for item in findings}
                self.assertTrue({"secret-pattern", "python-eval", "tls-verify-disabled", "container-privileged", "terraform-public-ingress", "dependency-advisory"} <= rules)
                store = m.Store(self.state)
                try:
                    service = m.assess_service(url, scope, store, scope_id)
                finally:
                    store.close()
                self.assertEqual(Handler.hits, 1)
                self.assertEqual({f["rule"] for f in service[1]}, {"header-csp", "header-nosniff"})
                self.assertTrue(all(item["remediation"] and item["evidence"] and item["provenance"]["version"] for item in findings + service[1]))
                self.assertTrue(all(call.args[0] == "127.0.0.1" for call in resolver.call_args_list))

    def test_scope_refusal_pass_no_network_or_dns(self):
        scope, scope_id = m.scope_at(self.scope)
        store = m.Store(self.state)
        try:
            with patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")), patch.object(socket.socket, "connect", side_effect=AssertionError("connect forbidden")):
                for target in ["http://127.0.0.1:8765/", "https://8.8.8.8:443/", "https://192.168.1.2:443/", "https://example.com/", "http://169.254.169.254/"]:
                    with self.subTest(target=target), self.assertRaises(m.Refused):
                        m.assess_service(target, scope, store, scope_id)
        finally:
            store.close()

    def test_public_allowlist_rejected(self):
        self.scope_data["services"] = ["https://8.8.8.8:443/"]
        self.write_scope()
        with self.assertRaises(m.Refused):
            m.scope_at(self.scope)

    def test_secret_redaction_pass_all_generated_outputs(self):
        run_id = self.run_repo()
        exported = self.base / "report.michael"
        outputs = [self.cli("findings", "--run", run_id).stdout, self.cli("findings", "--run", run_id, "--format", "markdown").stdout]
        self.cli("export", "--run", run_id, "--output", str(exported))
        outputs.append(self.cli("findings", "--from-export", str(exported)).stdout)
        for field in ["Confidence:", "Observed at:", "Provenance:", "Evidence:", "Remediation:"]:
            self.assertIn(field, outputs[1])
        for content in outputs:
            self.assertNotIn(self.seed, content)
        for path in [*self.state.iterdir(), exported]:
            self.assertNotIn(self.seed.encode(), path.read_bytes())
        store = m.Store(self.state)
        try:
            self.assertNotIn(self.seed, json.dumps(store.records))
        finally:
            store.close()

    def test_records_encrypted_and_tampering_refused(self):
        self.run_repo()
        record = self.state / "00000000.record"
        original = record.read_bytes()
        self.assertNotIn(b"assessment-start", original)
        self.assertEqual(record.stat().st_mode & 0o777, 0o400)
        record.chmod(0o600)
        record.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        self.cli("findings", expected=2)

    def test_aggregate_inventory_limit_preserves_readable_state(self):
        previous = self.run_repo()
        packages = {str(i): {"version": "1"} for i in range(2000)}
        lockfile = json.dumps({"packages": packages}, separators=(",", ":"))
        for i in range(160):
            directory = self.repo / ("project-" + str(i))
            directory.mkdir()
            (directory / "package-lock.json").write_text(lockfile)
        self.assertLess(160 * len(lockfile), m.MAX_TOTAL)
        result = self.cli("assess-repo", str(self.repo), expected=2)
        self.assertIn("inventory_limit", result.stderr)
        self.assertEqual(json.loads(self.cli("findings").stdout)["run_id"], previous)
        store = m.Store(self.state)
        try:
            self.assertEqual(store.records[-1]["payload"]["kind"], "refusal_or_failure")
        finally:
            store.close()

    def test_oversized_record_and_export_are_refused_before_creation(self):
        store = m.Store(self.state)
        try:
            store.append({"kind": "fixture"})
            oversized = {"kind": "fixture", "padding": "x" * (m.MAX_REPORT * 4)}
            with self.assertRaises(m.Refused):
                store.append(oversized)
            self.assertEqual(len(list(self.state.glob("*.record"))), 1)
            destination = self.base / "oversized.michael"
            with self.assertRaises(m.Refused):
                store.export(oversized, destination)
            self.assertFalse(destination.exists())
        finally:
            store.close()
        reopened = m.Store(self.state)
        try:
            self.assertEqual(len(reopened.records), 1)
        finally:
            reopened.close()

    def test_credentials_in_filenames_and_directories_are_not_disclosed(self):
        filename_token = "xoxb-" + secrets.token_hex(24)
        directory_token = "private-" + secrets.token_hex(24)
        directory = self.repo / directory_token
        directory.mkdir()
        (directory / (filename_token + ".py")).write_text("eval(user_input)\n")
        run_id = self.run_repo()
        exported = self.base / "path-redaction.michael"
        self.cli("export", "--run", run_id, "--output", str(exported))
        outputs = [self.cli("findings").stdout,
                   self.cli("findings", "--format", "markdown").stdout,
                   self.cli("findings", "--from-export", str(exported)).stdout]
        store = m.Store(self.state)
        try:
            outputs.append(json.dumps(store.records))
        finally:
            store.close()
        for token in (filename_token, directory_token):
            self.assertTrue(all(token not in output for output in outputs),
                            "credential disclosed through repository-controlled path")
            self.assertTrue(all(token.encode() not in path.read_bytes()
                                for path in [*self.state.iterdir(), exported]),
                            "credential disclosed in persisted output")

    def test_export_does_not_overwrite(self):
        self.run_repo()
        output = self.base / "existing"
        output.write_bytes(b"keep")
        self.cli("export", "--output", str(output), expected=2)
        self.assertEqual(output.read_bytes(), b"keep")

    def test_audit_sequence_gap_refused(self):
        self.run_repo()
        (self.state / "00000000.record").rename(self.base / "moved.record")
        self.cli("findings", expected=2)

    def test_refusal_audited_and_arguments_not_echoed(self):
        self.cli("assess-service", "https://user:" + self.seed + "@8.8.8.8/", expected=2)
        store = m.Store(self.state)
        try:
            self.assertEqual(store.records[-1]["payload"]["kind"], "refusal_or_failure")
            self.assertNotIn(self.seed, json.dumps(store.records))
        finally:
            store.close()
        self.cli("--unknown-argument", self.seed, expected=2)

    def test_symlink_and_out_of_scope_repository_refused(self):
        link = self.base / "link"
        link.symlink_to(self.repo)
        self.cli("assess-repo", str(link), expected=2)
        self.cli("assess-repo", str(self.base), expected=2)
        (self.repo / "outside.py").symlink_to(self.report)
        self.assertNotIn(self.seed, self.cli("assess-repo", str(self.repo)).stdout)

    def test_secret_stores_and_environment_files_are_not_read(self):
        (self.repo / ".env").symlink_to(self.report)
        (self.repo / "auth.json").write_text("not even valid JSON")
        original = m.read_regular
        def checked(path, *args, **kwargs):
            self.assertNotIn(Path(path).name, {".env", "auth.json"})
            return original(path, *args, **kwargs)
        with patch.object(m, "read_regular", checked):
            m.scan_repo(str(self.repo), self.scope_data)

    def test_remediation_history(self):
        self.run_repo()
        before = json.loads(self.cli("findings").stdout)
        (self.repo / "example.py").write_text("safe = True\n")
        self.run_repo()
        after = json.loads(self.cli("findings").stdout)
        self.assertEqual(after["history"]["previous_run"], before["run_id"])
        self.assertTrue(after["history"]["no_longer_observed"])
        self.assertTrue(after["history"]["still_observed"])

    def test_skipped_checks_are_visible(self):
        self.cli("assess-repo", str(self.repo))
        run = json.loads(self.cli("findings").stdout)
        statuses = {c["check"]: c["status"] for c in run["checks"]}
        self.assertEqual(statuses["known-vulnerabilities"], "skipped")
        self.assertEqual(statuses["external-source-lint"], "skipped")
        self.assertEqual(run["status"], "completed_with_limits")

    def test_redirect_is_never_followed(self):
        class Redirect(Handler):
            redirect = True
        with self.server(Redirect) as url:
            self.scope_data["services"] = [url]
            self.write_scope()
            self.cli("assess-service", url)
            run = json.loads(self.cli("findings").stdout)
            self.assertIn("redirect", {f["rule"] for f in run["findings"]})
            self.assertEqual(Redirect.hits, 1)

    def test_rate_limit_persists_across_processes(self):
        with self.server() as url:
            self.scope_data["services"] = [url]
            self.write_scope()
            self.cli("assess-service", url)
            result = self.cli("assess-service", url, expected=2)
            self.assertIn("rate_limited", result.stderr)
            self.assertEqual(Handler.hits, 1)

    def test_wall_deadline_bounds_slow_response(self):
        class Slow(Handler):
            def do_HEAD(self):
                try:
                    for byte in b"HTTP/1.1 200 OK\r\n":
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.15)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        with self.server(Slow) as url:
            self.scope_data["services"] = [url]
            self.write_scope()
            before = time.monotonic()
            with patch.object(m, "NETWORK_TIMEOUT", 0.35):
                store = m.Store(self.state)
                try:
                    with self.assertRaises(m.Refused):
                        m.assess_service(url, self.scope_data, store, "fixture")
                finally:
                    store.close()
            self.assertLess(time.monotonic() - before, 1)

    def test_private_lab_requires_tls_and_verification(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        with self.assertRaises(m.Refused):
            m.service_url("http://192.168.1.3:80/")
        self.assertEqual(m.service_url("https://192.168.1.3:443/")[1], "192.168.1.3")
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Michael test fixture")])
        now = datetime.now(timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                       .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
                       .not_valid_after(now + timedelta(hours=1)).add_extension(x509.SubjectAlternativeName([
                           x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False).sign(key, hashes.SHA256()))
        key_file, cert_file = self.base / "fixture.key", self.base / "fixture.pem"
        key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_file, key_file)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        Handler.hits = 0
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"https://127.0.0.1:{server.server_port}/"
            self.scope_data["services"] = [url]
            self.write_scope()
            self.cli("assess-service", url, expected=2)
            self.assertEqual(Handler.hits, 0)
            store = m.Store(self.state)
            try:
                self.assertEqual(store.records[-1]["payload"]["kind"], "refusal_or_failure")
            finally:
                store.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_hardlinked_files_are_not_read(self):
        external = self.base / "external.py"
        external.write_text("password = '" + self.seed + "'\n")
        os.link(external, self.repo / "linked.py")
        result = m.scan_repo(str(self.repo), self.scope_data)
        self.assertFalse(any(f["evidence"]["location"] == m.path_label("linked.py") for f in result[1]))

    def test_response_content_and_header_values_discarded(self):
        seed = self.seed
        class SensitiveResponse(Handler):
            def do_HEAD(self):
                self.send_response(200)
                self.send_header("X-Fixture-Secret", seed)
                self.end_headers()
                self.wfile.write(seed.encode())
        with self.server(SensitiveResponse) as url:
            self.scope_data["services"] = [url]
            self.write_scope()
            self.cli("assess-service", url)
            self.assertNotIn(seed, self.cli("findings").stdout)
            store = m.Store(self.state)
            try:
                self.assertNotIn(seed, json.dumps(store.records))
            finally:
                store.close()

    def test_busy_state_refused_without_waiting(self):
        store = m.Store(self.state)
        try:
            start = time.monotonic()
            self.cli("findings", expected=2)
            self.assertLess(time.monotonic() - start, 1)
        finally:
            store.close()

    def test_malformed_import_does_not_leak(self):
        self.report.write_text(self.seed)
        self.cli("assess-repo", str(self.repo), "--trivy-report", str(self.report), expected=2)

    def test_external_credential_paths_refused_before_read(self):
        for filename in ["auth.json", ".env", "identity.key", "identity.pem", ".ssh/config", ".aws/custom.json"]:
            path = self.base / filename
            path.parent.mkdir(exist_ok=True)
            path.write_text(self.seed)
            with self.subTest(filename=filename), patch.object(m, "read_regular", side_effect=AssertionError("must refuse before opening")):
                with self.assertRaises(m.Refused):
                    m.load_json(path)
                with self.assertRaises(m.Refused):
                    m.trivy_report(path, "fixture")
                with self.assertRaises(m.Refused):
                    m.Store.import_export(None, path)
        alias = self.base / "alias"
        alias.symlink_to(self.base / ".ssh", target_is_directory=True)
        with self.assertRaises(OSError):
            m.read_external(alias / "config")

    def test_manifest_and_skills_are_coherent(self):
        manifest = tomllib.loads((PACKAGE / "plugin.toml").read_text())
        self.assertEqual(manifest["id"], "michael")
        self.assertEqual(manifest["capabilities"], [])
        self.assertEqual(len(manifest["commands"]), 4)
        for command in manifest["commands"]:
            content = (PACKAGE / manifest["skills"] / command["skill"] / "SKILL.md").read_text()
            self.assertIn("name: " + command["skill"], content)
            self.assertIn("scope", content)
            self.assertIn("untested", content)


if __name__ == "__main__":
    unittest.main()
