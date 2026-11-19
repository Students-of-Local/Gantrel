# Michael

Michael is a Local Studio plugin for defensive assessment of repositories and explicitly authorized local or private lab web services. It records what was checked, what was skipped, and why each finding needs review. It never fixes the target.

This package is independent of Gantrel's printing plugin. Copying `plugins/michael/` is sufficient to package it; it does not import Gantrel or call Fonsviva or a model. Local Studio supplies the model and shell tools for its declarative commands. Read [SUPPORTED.md](SUPPORTED.md) before relying on a check.

## Install

Requires Linux, Python 3.11 or later, and an already available `cryptography` Python module. Optional: an already installed Gitleaks executable at `~/.local/bin/gitleaks`. Michael does not download scanners, vulnerability databases, Python packages, or updates. If a dependency is absent, stop and arrange installation separately with the owner.

Copy this folder into a new directory named `michael` under the Local Studio plugin directory. Do not overwrite an existing plugin:

```sh
test ! -e "$HOME/.local-studio/plugins/michael" && cp -R plugins/michael "$HOME/.local-studio/plugins/michael"
local-studio plugin validate "$HOME/.local-studio/plugins/michael"
```

The manifest declares four skills and four commands: `/michael-repo`, `/michael-service`, `/michael-findings`, and `/michael-export`. The validator should print `OK michael 0.1.0 (4 skills, 0 prompts, 4 commands)`. An older GUI executable may lack the plugin CLI; use the matching Local Studio core CLI. Validation proves packaging, not a successful model-led assessment.

Use `python3 /path/to/michael/michael.py --help` for the runner. There is no dashboard, background daemon, browser extension, account, telemetry, shell hook, or provider key.

## First safe assessment

Create a new empty scratch directory. The following Python creates only a deliberately weak owned fixture and scope. Run it there, outside any real repository:

```sh
python3 - <<'PY'
import json
from pathlib import Path
repo = Path('fixture').absolute()
repo.mkdir()
(repo / 'sample.py').write_text('eval(user_input)\n')
(repo / 'compose.yml').write_text('privileged: true\n')
Path('scope.json').write_text(json.dumps({
    'version': 1, 'owned_or_authorized': True,
    'repositories': [str(repo)], 'services': []
}))
PY
python3 /path/to/michael/michael.py --state ./private-state --scope ./scope.json assess-repo "$PWD/fixture"
python3 /path/to/michael/michael.py --state ./private-state findings --format markdown
python3 /path/to/michael/michael.py --state ./private-state export --output ./assessment.michael
python3 /path/to/michael/michael.py --state ./private-state findings --from-export ./assessment.michael
```

The first command reports the run id and finding count. The next shows evidence locations, check statuses and remediation. All stored run/audit records and exports are AES-256-GCM encrypted. Terminal findings are decrypted in memory; keep the terminal private and do not redirect them into plaintext files. Exports require the originating state directory's report key. Michael never displays that key.

For a service you own, list its exact canonical URL in the scope, such as `http://127.0.0.1:8765/`. Start only your own loopback fixture separately, then run:

```sh
python3 /path/to/michael/michael.py --state ./private-state --scope ./scope.json assess-service http://127.0.0.1:8765/
```

HTTP outside loopback is refused. Private lab HTTPS must have a trusted certificate valid for its literal IP address. A self-signed untrusted certificate is refused. See [AUTHORIZATION.md](AUTHORIZATION.md) for full boundaries.

## Offline scanner reports

An existing Trivy JSON schema 2 report can be imported during a repository run:

```sh
python3 /path/to/michael/michael.py --state ./private-state --scope ./scope.json assess-repo /absolute/owned/repo --trivy-report /absolute/authorized/offline-trivy.json
```

This imports a historical snapshot. Michael does not execute Trivy, update its database, verify that its report matches the current tree, or interpret its untrusted descriptions. It retains only validated advisory identifiers, severity, package fingerprints and report provenance. A dependency fingerprint is the first 16 hex characters of SHA-256 of the report's `PkgName`; inventory package ids use the package-lock entry key. Compare locally against the original report to identify a dependency. No raw scanner message, snippet or credential field is stored. Database freshness remains unknown. Absence of an imported report is an explicit skipped check.

## Evidence locations and size limits

Repository evidence identifies files by `file-` followed by the first 24 hex characters of SHA-256 of the filesystem-encoded relative path. Filenames and directory names can themselves contain credentials, so Michael retains no path mapping. To match a location, hash a relative path you already know locally and compare the identifier; use the recorded line number within that file. Dependency inventory is capped at 2000 entries across the whole assessment. Oversized inventories, records and exports are refused before an unreadable record or output file is created.

## Recovery

Exit 0 means the requested operation completed. Exit 2 means refused or failed; its fixed reason contains no raw target argument or underlying exception. Keep the encrypted records when investigating a refusal. Do not broaden scope, disable TLS verification, or fall back to an unscoped scanner.

The state directory must be owned by the current user with mode 0700 and no symlink parents. A simultaneous process using that state is refused immediately. Use the same state for service runs so its persistent one-request-per-second limiter applies. Do not delete audit records or change their names. Authentication or sequence failures block further operations. Restore the whole state from a trusted backup, or start a new private state directory and keep the old one for investigation. Back up the entire state securely; losing its generated report key makes records and exports unreadable. Copy no host credentials into it.

Only the selected state and an explicitly new export path are written. Assessments never modify target files. The state is encrypted against disclosure of record files without the key; its key is protected by filesystem permissions, not a separate hardware security boundary. A filesystem owner or root with the key can rewrite or truncate the history. Michael provides create-only authenticated records and detects changed records and sequence gaps; it cannot detect removal of the final record or deletion of an entire history without an external anchor.

## Originality and references

The operator concepts studied from [ARTEX](https://github.com/BaaRai/ARTEX/tree/a13c68cb7078d43b66fb398db3d610e513b6d77a) were asset inventory, task status, findings, evidence, and remediation history. Only its README taxonomy was consulted. No ARTEX code, prompts, styles or assets are included. Michael uses original code and instructions under Gantrel's MIT license.

The Local Studio contract was read at `e9d1a69801fa5bfeef435de2362352bb8cb96bdd`. Scanner adapter references: [Gitleaks](https://github.com/gitleaks/gitleaks) stdin JSON with redaction, [Trivy](https://github.com/aquasecurity/trivy) JSON schema 2. These are compatibility references, not claims that either scanner or database was installed or current.
