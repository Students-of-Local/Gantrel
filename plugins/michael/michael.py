#!/usr/bin/env python3
"""Michael: bounded local security assessments, with encrypted create-only records."""
import argparse
import contextlib
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import resource
import socket
import ssl
import stat
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

VERSION = "0.1.0"
MAX_FILE = 1024 * 1024
MAX_FILES = 2000
MAX_REPORT = 4 * MAX_FILE
MAX_RECORD = MAX_REPORT * 4
MAX_INVENTORY = 2000
MAX_FINDINGS = 2000
NETWORK_TIMEOUT = 3
MAX_TOTAL = 16 * MAX_FILE
MAX_LINES = 100000
SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"}
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    ".ssh",
    ".aws",
    ".codex",
    ".gnupg",
    ".kube",
    ".password-store",
    "__pycache__",
}
SENSITIVE = {"auth.json", "credentials", "credentials.json", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}
SOURCE = {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".yaml", ".yml", ".json", ".toml", ".tf", ".txt", ".conf", ".ini", ".sh"}
TOKEN = re.compile(r"(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|sk-(?:proj-)?[A-Za-z0-9_-]{20,})")
ASSIGNMENT = re.compile(r"(?i)\b(?:api[_-]?key|secret|password|access[_-]?token)\s*[=:]\s*['\"]?([^\s'\"#,;]{8,})")
RULES = {
    "secret-pattern": ("high", "medium", "Rotate the credential if real, remove it from source and history, and use an OS credential manager."),
    "python-eval": ("medium", "low", "Review dynamic evaluation; parse structured input instead of evaluating it."),
    "tls-verify-disabled": ("high", "medium", "Enable certificate and hostname verification with a trusted certificate authority."),
    "container-privileged": ("high", "medium", "Remove privileged mode and grant only the individual capabilities required."),
    "terraform-public-ingress": ("medium", "low", "Restrict ingress to the authorized private network and review the exposed ports."),
    "dependency-advisory": ("high", "medium", "Review the imported advisory against the installed package and upgrade to a fixed supported release."),
    "header-csp": ("low", "medium", "Set a Content-Security-Policy appropriate to this application."),
    "header-nosniff": ("low", "medium", "Set X-Content-Type-Options to nosniff."),
    "header-hsts": ("low", "medium", "Set Strict-Transport-Security after confirming HTTPS works for the intended host."),
    "redirect": ("low", "high", "Review the redirect destination separately; Michael did not follow it."),
}


class Refused(Exception):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def stamp():
    return datetime.now(timezone.utc).isoformat()


def read_regular(path, limit=MAX_FILE, dir_fd=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise Refused("file_type_or_size")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise Refused("file_size")
        return data


def read_external(path, limit=MAX_FILE):
    source = Path(path).absolute()
    sensitive_dirs = {".ssh", ".codex", ".aws", ".gnupg", ".kube", ".password-store"}
    if (source.name in SENSITIVE or source.name.startswith(".env")
            or source.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}
            or any(part in sensitive_dirs for part in source.parts)):
        raise Refused("sensitive_file_excluded")
    directory_fd = open_directory(source.parent)
    try:
        return read_regular(source.name, limit, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


def decode_json(data):
    try:
        return json.loads(data)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise Refused("invalid_json") from error


def load_json(path, limit=MAX_FILE):
    return decode_json(read_external(path, limit))


def service_url(value):
    if not isinstance(value, str) or len(value) > 256:
        raise Refused("invalid_service")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise Refused("invalid_service")
        ip = ipaddress.ip_address(parsed.hostname or "")
        networks = [ipaddress.ip_network(x) for x in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")]
        if not ip.is_loopback and not any(ip in network for network in networks):
            raise Refused("public_or_special_address")
        if parsed.scheme == "http" and not ip.is_loopback:
            raise Refused("private_lab_requires_tls")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if not re.fullmatch(r"/[A-Za-z0-9_./~-]*", path) or ".." in path or not 1 <= port <= 65535:
            raise Refused("invalid_service")
        host = f"[{ip}]" if ip.version == 6 else str(ip)
        normalized = f"{parsed.scheme}://{host}:{port}{path}"
        return normalized, str(ip), port, path, parsed.scheme
    except (ValueError, TypeError) as error:
        raise Refused("literal_private_ip_required") from error


def scope_at(path):
    scope = load_json(path)
    if not isinstance(scope, dict) or set(scope) != {"version", "owned_or_authorized", "repositories", "services"}:
        raise Refused("invalid_scope")
    if scope["version"] != 1 or scope["owned_or_authorized"] is not True:
        raise Refused("owner_authorization_required")
    for field in ("repositories", "services"):
        if not isinstance(scope[field], list) or len(scope[field]) > 32:
            raise Refused("invalid_scope")
    for repo in scope["repositories"]:
        if not isinstance(repo, str) or not Path(repo).is_absolute() or str(Path(repo).resolve()) != repo:
            raise Refused("canonical_repository_required")
    for url in scope["services"]:
        if service_url(url)[0] != url:
            raise Refused("canonical_service_required")
    return scope, digest(canonical(scope))


def create_file(path, data, mode=0o400):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


class Store:
    def __init__(self, root):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self.root = Path(root).absolute()
        if any(p.is_symlink() for p in [self.root, *self.root.parents]):
            raise Refused("state_symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Refused("state_must_be_private")
        self.lockfd = os.open(self.root / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        if not stat.S_ISREG(os.fstat(self.lockfd).st_mode):
            raise Refused("invalid_state_lock")
        fcntl.flock(self.lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        keypath = self.root / "report.key"
        if not keypath.exists():
            create_file(keypath, AESGCM.generate_key(bit_length=256))
        info = keypath.lstat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise Refused("invalid_report_key")
        self.cipher = AESGCM(read_regular(keypath, 32))
        self.records = []
        previous = "0" * 64
        for index, path in enumerate(sorted(self.root.glob("*.record"))):
            if path.name != f"{index:08d}.record":
                raise Refused("audit_sequence")
            blob = read_regular(path, MAX_RECORD)
            try:
                record = json.loads(self.cipher.decrypt(blob[:12], blob[12:], path.name.encode()))
            except Exception as error:
                raise Refused("audit_integrity") from error
            if record.get("previous") != previous or record.get("sequence") != index:
                raise Refused("audit_chain")
            self.records.append(record)
            previous = digest(blob)
        self.previous = previous

    def close(self):
        if hasattr(self, "lockfd"):
            os.close(self.lockfd)
            del self.lockfd

    def append(self, payload):
        record = {"sequence": len(self.records), "previous": self.previous, "recorded_at": stamp(), "payload": payload}
        name = f"{len(self.records):08d}.record"
        blob = self.seal(record, name.encode())
        create_file(self.root / name, blob)
        self.records.append(record)
        self.previous = digest(blob)
        return record

    def seal(self, payload, associated_data, prefix=b""):
        plaintext = canonical(payload)
        if len(prefix) + 12 + len(plaintext) + 16 > MAX_RECORD:
            raise Refused("record_size_limit")
        nonce = secrets.token_bytes(12)
        return prefix + nonce + self.cipher.encrypt(nonce, plaintext, associated_data)

    def runs(self):
        return [record["payload"] for record in self.records if record["payload"].get("kind") == "assessment"]

    def find(self, run_id):
        runs = self.runs()
        if run_id is None and runs:
            return runs[-1]
        for run in runs:
            if run["run_id"] == run_id:
                return run
        raise Refused("run_not_found")

    def export(self, run, destination):
        blob = self.seal(run, b"michael-export-v1", b"MICHAEL1")
        create_file(Path(destination), blob)

    def import_export(self, path):
        blob = read_external(path, MAX_RECORD)
        if blob[:8] != b"MICHAEL1":
            raise Refused("invalid_export")
        try:
            return json.loads(self.cipher.decrypt(blob[8:20], blob[20:], b"michael-export-v1"))
        except Exception as error:
            raise Refused("export_integrity") from error


def path_label(relative):
    return "file-" + digest(os.fsencode(relative))[:24]


def finding(rule, asset, location, line, provenance, severity=None, advisory=None):
    level, confidence, remediation = RULES[rule]
    identity = [rule, asset, location, line, advisory]
    return {"id": digest(canonical(identity))[:24], "rule": rule, "severity": severity or level,
            "asset": asset, "evidence": {"location": location, "line": line, "advisory": advisory},
            "confidence": confidence, "remediation": remediation, "observed_at": stamp(), "provenance": provenance}



def open_directory(path):
    directory_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(path).parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


@contextlib.contextmanager
def secure_walk(root):
    directory_fd = open_directory(root)
    try:
        yield os.fwalk(".", follow_symlinks=False, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


def scan_repo(target, scope, report_path=None):
    raw = Path(target)
    if any(p.is_symlink() for p in [raw.absolute(), *raw.absolute().parents]):
        raise Refused("repository_symlink")
    root = raw.resolve()
    if str(root) not in scope["repositories"] or not root.is_dir():
        raise Refused("repository_not_allowlisted")
    asset = "repo-" + digest(str(root).encode())[:16]
    provenance = {"tool": "michael-local-rules", "version": VERSION}
    findings, inventory = [], []
    source_chunks, source_map = [], []
    total_bytes = 0
    skipped = 0
    seen = 0
    directories = 0
    with secure_walk(root) as walk:
        for current, dirs, files, directory_fd in walk:
            directories += len(dirs)
            if directories > MAX_FILES:
                raise Refused("repository_directory_limit")
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (root / current / d).is_symlink())
            for name in sorted(files):
                seen += 1
                if seen > MAX_FILES:
                    raise Refused("repository_file_limit")
                path = root / current / name
                if path.is_symlink() or name in SENSITIVE or name.startswith(".env") or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
                    skipped += 1
                    continue
                if path.suffix.lower() not in SOURCE and name != "Dockerfile":
                    skipped += 1
                    continue
                try:
                    data = read_regular(name, dir_fd=directory_fd)
                except (OSError, Refused):
                    skipped += 1
                    continue
                if b"\x00" in data:
                    skipped += 1
                    continue
                total_bytes += len(data)
                if total_bytes > MAX_TOTAL:
                    raise Refused("repository_byte_limit")
                text = data.decode("utf-8", errors="replace")
                relative = path_label(path.relative_to(root))
                if len(source_map) + len((text + "\n").splitlines()) > MAX_LINES:
                    raise Refused("repository_line_limit")
                source_chunks.append(text + "\n")
                source_map.extend((relative, i) for i in range(1, len((text + "\n").splitlines()) + 1))
                if name == "package-lock.json":
                    try:
                        lock = json.loads(text)
                        packages = lock.get("packages", {})
                        if not isinstance(packages, dict):
                            raise ValueError()
                        for package, metadata in list(packages.items())[:MAX_FILES]:
                            if not isinstance(metadata, dict) or not isinstance(package, str):
                                continue
                            version = metadata.get("version", "")
                            if isinstance(version, str) and re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,8}){0,3}", version):
                                if len(inventory) >= MAX_INVENTORY:
                                    raise Refused("inventory_limit")
                                inventory.append({"id": digest(package.encode())[:16], "ecosystem": "npm", "version": version})
                    except (ValueError, AttributeError, RecursionError):
                        skipped += 1
                for number, line in enumerate(text.splitlines(), 1):
                    rules = []
                    if TOKEN.search(line) or ASSIGNMENT.search(line):
                        rules.append("secret-pattern")
                    if path.suffix == ".py" and re.search(r"\beval\s*\(", line):
                        rules.append("python-eval")
                    if re.search(r"\bverify\s*=\s*False\b", line):
                        rules.append("tls-verify-disabled")
                    if path.suffix in {".yaml", ".yml"} and re.search(r"\bprivileged\s*:\s*true\b", line, re.I):
                        rules.append("container-privileged")
                    if path.suffix == ".tf" and "0.0.0.0/0" in line:
                        rules.append("terraform-public-ingress")
                    for rule in rules:
                        findings.append(finding(rule, asset, relative, number, provenance))
                    if len(findings) > MAX_FINDINGS:
                        raise Refused("finding_limit")
    checks = [{"check": "bounded-local-rules", "status": "completed", "files_considered": seen, "files_skipped": skipped,
               "provenance": provenance}, {"check": "npm-lock-inventory", "status": "completed" if inventory else "skipped", "reason": "metadata_only_no_resolution"},
              {"check": "known-vulnerabilities", "status": "skipped", "reason": "offline_report_not_supplied"},
              {"check": "external-source-lint", "status": "skipped", "reason": "no_external_linter_executed"}]
    external, scanner_check = gitleaks("".join(source_chunks).encode(), source_map, asset, root)
    findings.extend(external)
    checks.append(scanner_check)
    if report_path:
        imported, origin = trivy_report(report_path, asset)
        findings.extend(imported)
        if len(findings) > MAX_FINDINGS:
            raise Refused("finding_limit")
        checks[2] = {"check": "known-vulnerabilities", "status": "imported", "reason": "historical_unverified_against_current_tree", "provenance": origin}
    return asset, findings, checks, inventory



def gitleaks(data, locations, asset, assessed_root):
    executable = Path.home() / ".local/bin/gitleaks"
    if not executable.is_file():
        return [], {"check": "gitleaks", "status": "skipped", "reason": "trusted_local_binary_absent"}
    executable = executable.resolve()
    if executable.is_relative_to(assessed_root):
        return [], {"check": "gitleaks", "status": "skipped", "reason": "binary_inside_assessed_repository"}
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C", "HOME": "/nonexistent"}
    version = subprocess.run([str(executable), "version"], cwd="/", env=environment,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3, check=False).stdout
    if not re.fullmatch(rb"[0-9]+\.[0-9]+\.[0-9]+\s*", version):
        return [], {"check": "gitleaks", "status": "failed", "reason": "invalid_tool_version"}
    origin = {"tool": "gitleaks", "version": version.decode().strip(), "config_sha256": digest(read_regular(Path(__file__).with_name("gitleaks.toml")))}
    fd = os.memfd_create("michael-scanner", 0)
    try:
        def limits():
            resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_REPORT, MAX_REPORT))
        command = [str(executable), "stdin", "--config", str(Path(__file__).with_name("gitleaks.toml").resolve()),
                   "--gitleaks-ignore-path", "/dev/null", "--ignore-gitleaks-allow", "--redact=100", "--no-banner",
                   "--no-color", "--max-decode-depth", "0", "--timeout", "8", "--report-format", "json",
                   "--report-path", f"/proc/self/fd/{fd}"]
        result = subprocess.run(command, input=data, cwd="/", env=environment, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=10, pass_fds=(fd,), preexec_fn=limits, check=False)
        if result.returncode not in {0, 1}:
            return [], {"check": "gitleaks", "status": "failed", "reason": "scanner_error", "provenance": origin}
        os.lseek(fd, 0, os.SEEK_SET)
        report = json.loads(os.read(fd, MAX_REPORT + 1) or b"[]")
        if not isinstance(report, list) or len(report) > MAX_FINDINGS:
            raise Refused("scanner_report_limit")
        findings = []
        for item in report:
            line = item.get("StartLine") if isinstance(item, dict) else None
            if not isinstance(line, int) or not 1 <= line <= len(locations):
                raise Refused("scanner_report_invalid")
            location, number = locations[line - 1]
            findings.append(finding("secret-pattern", asset, location, number, origin))
        return findings, {"check": "gitleaks", "status": "completed", "provenance": origin}
    except subprocess.TimeoutExpired:
        return [], {"check": "gitleaks", "status": "failed", "reason": "scanner_timeout", "provenance": origin}
    finally:
        os.close(fd)


def trivy_report(path, asset):
    report_data = read_external(path, MAX_REPORT)
    report = decode_json(report_data)
    if not isinstance(report, dict) or report.get("SchemaVersion") != 2:
        raise Refused("invalid_offline_report")
    metadata = report.get("Metadata", {})
    origin = {"tool": "trivy-offline-import", "version": VERSION, "report_sha256": digest(report_data),
              "scanner_version": "not_recorded", "database_freshness": "unknown"}
    if isinstance(metadata, dict):
        version = metadata.get("TrivyVersion", "")
        if isinstance(version, str) and re.fullmatch(r"[0-9]{1,8}\.[0-9]{1,8}\.[0-9]{1,8}", version):
            origin["scanner_version"] = version
    results = report.get("Results", [])
    if not isinstance(results, list) or len(results) > 1000:
        raise Refused("invalid_offline_report")
    findings = []
    for result in results:
        if not isinstance(result, dict):
            raise Refused("invalid_offline_report")
        vulnerabilities = result.get("Vulnerabilities") or []
        if not isinstance(vulnerabilities, list) or len(vulnerabilities) > MAX_FINDINGS:
            raise Refused("invalid_offline_report")
        for entry in vulnerabilities:
            if not isinstance(entry, dict):
                raise Refused("invalid_offline_report")
            advisory = entry.get("VulnerabilityID", "")
            severity = entry.get("Severity", "UNKNOWN")
            if not isinstance(advisory, str) or not re.fullmatch(r"(?:CVE-[0-9]{4}-[0-9]{4,10}|GHSA-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4})", advisory):
                continue
            if severity not in SEVERITIES:
                severity = "UNKNOWN"
            package = entry.get("PkgName", "")
            if not isinstance(package, str):
                raise Refused("invalid_offline_report")
            location = "dependency-" + digest(package.encode())[:16]
            item = finding("dependency-advisory", asset, location, None, origin, severity.lower(), advisory)
            if item["id"] not in {f["id"] for f in findings}:
                findings.append(item)
            if len(findings) > MAX_FINDINGS:
                raise Refused("finding_limit")
    return findings, origin


def assess_service(target, scope, store, scope_id):
    normalized, ip, port, path, scheme = service_url(target)
    if normalized not in scope["services"]:
        raise Refused("service_not_allowlisted")
    asset = "service-" + digest(normalized.encode())[:16]
    prior = [r["payload"].get("network_at", 0) for r in store.records if r["payload"].get("kind") == "network-attempt"]
    if prior and time.time() - max(prior) < 1:
        raise Refused("rate_limited")
    store.append({"kind": "network-attempt", "scope_id": scope_id, "asset": asset, "network_at": time.time()})
    connection = None
    def deadline(signum, frame):
        raise Refused("network_deadline")
    previous_handler = signal.signal(signal.SIGALRM, deadline)
    signal.setitimer(signal.ITIMER_REAL, NETWORK_TIMEOUT)
    try:
        conn_type = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        options = {"timeout": NETWORK_TIMEOUT}
        if scheme == "https":
            options["context"] = ssl.create_default_context()
        connection = conn_type(ip, port, **options)
        connection.request("HEAD", path, headers={"User-Agent": "Michael/" + VERSION, "Connection": "close"})
        response = connection.getresponse()
        names = {name.lower() for name, _ in response.getheaders()}
        status = response.status
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if connection:
            connection.close()
    provenance = {"tool": "python-http-client", "version": ".".join(map(str, sys.version_info[:3])), "tls": "verified" if scheme == "https" else "loopback-http"}
    findings = []
    for header, rule in [("content-security-policy", "header-csp"), ("x-content-type-options", "header-nosniff")]:
        if header not in names:
            findings.append(finding(rule, asset, "response-headers", None, provenance))
    if scheme == "https" and "strict-transport-security" not in names:
        findings.append(finding("header-hsts", asset, "response-headers", None, provenance))
    if 300 <= status < 400:
        findings.append(finding("redirect", asset, "response-status", None, provenance))
    checks = [{"check": "http-head", "status": "completed", "response_status": status, "requests": 1, "provenance": provenance},
              {"check": "tls-verification", "status": "completed" if scheme == "https" else "skipped", "reason": "loopback_fixture_http" if scheme == "http" else "system_trust_store"}]
    return asset, findings, checks, []


def finish_run(store, scope_id, asset, findings, checks, inventory, started):
    previous = next((r for r in reversed(store.runs()) if r["asset"] == asset and r["scope_id"] == scope_id), None)
    old = {f["id"] for f in previous["findings"]} if previous else set()
    findings = list({f["id"]: f for f in findings}.values())
    current = {f["id"] for f in findings}
    run = {"kind": "assessment", "schema": 1, "run_id": secrets.token_hex(12), "scope_id": scope_id, "asset": asset,
           "started_at": started, "finished_at": stamp(), "status": "completed_with_limits", "checks": checks,
           "findings": findings, "inventory": inventory, "provenance": {"tool": "michael", "version": VERSION},
           "history": {"previous_run": previous["run_id"] if previous else None, "new": sorted(current - old),
                       "still_observed": sorted(current & old), "no_longer_observed": sorted(old - current)}}
    store.append(run)
    return run


def markdown(run):
    lines = ["# Michael assessment", "", "Run: " + run["run_id"], "Asset: " + run["asset"], "Status: " + run["status"], "",
             "No finding does not establish security. Skipped and imported checks retain their limits.", ""]
    for check in run["checks"]:
        lines.append(f"- {check['check']}: {check['status']}")
    lines.append("")
    for item in run["findings"]:
        lines.extend([f"## {item['rule']} ({item['severity']})", "", "Finding: " + item["id"],
                      "Confidence: " + item["confidence"], "Observed at: " + item["observed_at"],
                      "Provenance: " + json.dumps(item["provenance"], sort_keys=True),
                      "Evidence: " + json.dumps(item["evidence"], sort_keys=True), "Remediation: " + item["remediation"], ""])
    return "\n".join(lines)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refused("invalid_arguments")


def main(argv=None):
    parser = Parser(description="Michael local assessments. No public targets, credentials or exploit execution.")
    parser.add_argument("--state", required=True)
    parser.add_argument("--scope")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    repo = commands.add_parser("assess-repo")
    repo.add_argument("target")
    repo.add_argument("--trivy-report")
    service = commands.add_parser("assess-service")
    service.add_argument("target")
    view = commands.add_parser("findings")
    view.add_argument("--run")
    view.add_argument("--format", choices=["json", "markdown"], default="json")
    view.add_argument("--from-export")
    export = commands.add_parser("export")
    export.add_argument("--run")
    export.add_argument("--output", required=True)
    store = None
    try:
        args = parser.parse_args(argv)
        store = Store(args.state)
        if args.command.startswith("assess-"):
            if not args.scope:
                raise Refused("scope_required")
            scope, scope_id = scope_at(args.scope)
            started = stamp()
            store.append({"kind": "assessment-start", "scope_id": scope_id, "operation": args.command})
            if args.command == "assess-repo":
                result = scan_repo(args.target, scope, args.trivy_report)
            else:
                result = assess_service(args.target, scope, store, scope_id)
            run = finish_run(store, scope_id, *result, started)
            print(json.dumps({"run_id": run["run_id"], "status": run["status"], "findings": len(run["findings"])}))
        elif args.command == "findings":
            run = store.import_export(args.from_export) if args.from_export else store.find(args.run)
            print(markdown(run) if args.format == "markdown" else json.dumps(run, sort_keys=True))
        elif args.command == "export":
            run = store.find(args.run)
            store.export(run, args.output)
            store.append({"kind": "export", "run_id": run["run_id"]})
            print(json.dumps({"status": "exported_encrypted", "run_id": run["run_id"]}))
        return 0
    except (Refused, OSError, ValueError, TypeError, KeyError, ImportError, http.client.HTTPException, ssl.SSLError, subprocess.SubprocessError) as error:
        reason = str(error) if isinstance(error, Refused) else "operation_failed"
        if store:
            with contextlib.suppress(Exception):
                store.append({"kind": "refusal_or_failure", "reason": reason})
        print(json.dumps({"status": "refused_or_failed", "reason": reason}), file=sys.stderr)
        return 2
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    sys.exit(main())
