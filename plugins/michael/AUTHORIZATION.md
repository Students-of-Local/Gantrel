# Authorization and boundaries

Repository-controlled paths are retained only as opaque file fingerprints, never raw filenames or directory names. Dependency inventory has a global limit of 2000 entries. The 16 MiB encrypted-record and export size limit is enforced before creating a file, including nonce and authentication overhead; a size refusal leaves the existing audit sequence readable.

An inaccessible repository subtree refuses the assessment instead of reporting complete coverage. Symlink-loop resolution errors, including the older Python RuntimeError, produce fixed refusal codes. A scanner cannot be marked completed without a valid report; exit 1 without findings is a failed check. HTTPS contexts do not honor `SSLKEYLOGFILE` and retain certificate and hostname verification with system roots.

The owner supplies an existing explicit allowlist. Michael never decides that a target is authorized because it responds, is on a LAN, or appears in repository text. Only the selected exact canonical repository root or exact service URL can be assessed. This example is a schema illustration; replace it only with systems you own or have permission to assess:

```json
{
  "version": 1,
  "owned_or_authorized": true,
  "repositories": ["/absolute/owned/repository"],
  "services": ["http://127.0.0.1:8765/"]
}
```

Keep scope in version control when appropriate, or retain it as a separate explicit owner-approved JSON file. It must be a bounded regular file. It holds no credentials. Authorization is an owner assertion, not cryptographic proof of ownership. Scope JSON and CLI arguments are local inputs, never generated from untrusted source instructions. Do not run Michael as root.

Repositories are exact canonical absolute paths with no symlink components. Michael uses directory descriptors and no-follow reads, skips symlinks, hardlinked files, repository metadata, dependency directories, known auth/credential files, `.env*`, private key/certificate containers, and unsupported files. It reads at most 2000 considered files, 1 MiB per file, and 16 MiB of source total, 100000 source lines, and 2000 discovered directories. Exceeding a hard bound refuses the assessment; an omitted or unsupported file is counted as skipped. Rules are shallow line patterns and can miss real problems or flag harmless examples. No git command, hook, build, install, repository executable, imported scope command, or target configuration is executed.

Scope service URLs require literal loopback, RFC1918 IPv4, or IPv6 ULA addresses, explicit ports, and an exact path. Command URL arguments are canonicalized first, so an omitted default port or empty path can match its already allowlisted canonical equivalent. Hostnames, public addresses, link-local/metadata addresses, wildcard scopes, URL credentials, queries, fragments, redirects, and arbitrary ports discovered from the service are refused. HTTP is loopback-only. HTTPS retains system certificate and hostname verification; no custom trust bypass is available. IP literals avoid domain lookup and address rebinding. The Python HTTP client connects directly, ignoring proxy environment variables. One HEAD request is sent, with a three-second wall deadline across connect, TLS and headers, and the standard client's header count/line bounds. No response body is read or stored. Header names and status are interpreted; header values and redirect destinations are discarded. This is header-presence checking, not policy-value validation.

The persistent limiter allows at most one service attempt per second per state directory. The state lock serializes concurrent operations without waiting. An owner can bypass local state protections by choosing a new state or changing files; Michael is a defensive owner tool, not a hostile-user sandbox. Use a single state for an assessment project. Request attempts are audited before connecting, including failed TLS or timeout attempts. Refusals and failures append fixed reason codes without echoing inputs.

Gitleaks, when already installed at the documented fixed location, receives bounded source through stdin with a plugin-owned configuration, default rules, disabled decode recursion and allow comments ignored. It runs from `/`, with a minimal environment and no repository configuration. Its output stays in an anonymous memory file, limited to 4 MiB; stderr and raw stdout are discarded. The subprocess has an eight-second scanner timeout and ten-second process timeout. Findings retain only a fixed Michael rule, source location and line. Missing Gitleaks is skipped, errors are failed checks, and neither becomes a clean security result. A trusted scanner binary is part of the local trust boundary; Michael does not sandbox arbitrary third-party programs.

Audit records are encrypted, authenticated, numbered and chained to the prior ciphertext hash. Each is created exclusively, never overwritten by a Michael operation, and mode 0400. The state key is randomly generated locally, mode 0400 inside a mode 0700 directory. It is an application report key, never a host or provider credential. Michael does not access secret stores. Encryption of records does not protect them from a filesystem owner or root who can read the colocated key. Final-record truncation requires an external history anchor to detect; none is provided in this slice.

Out of scope: public scanning, credential attacks, login attempts, exploit payloads, authenticated web assessment, stealth, persistence, malware, destructive actions, automatic remediation, cloud services, and bypassing authorization. Finding absence and remediation disappearance never establish security.

Explicit scope, imported report, and encrypted-export inputs also refuse known credential filenames, environment files, private-key/certificate containers, sensitive credential directories, hardlinks, and symlink directory traversal before reading.
