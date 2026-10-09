# Backup and restore audit remediation

Baseline: v0.9.6, commit `979b652b75cfe4f5e90570fa4a05c11f3d166e34`.

## Credential origin binding

Replacing settings with a key-free backup previously copied the current key by
provider ID alone. A backup with the same ID and a different origin could attach
that key to another service. Restoration now preserves an existing key only when
the normalized scheme, hostname, and effective port match. Same-origin path
changes still work, and an explicitly imported key retains its original meaning.
Incremental restoration continues to enforce the same origin constraint.

## Provider defaults

Both restore modes now reconcile `default_provider_by_model` against the final
provider binding set. Incremental restoration prefers valid local defaults;
replacement prefers valid imported defaults. Missing choices fall back to a
valid choice from the other snapshot, then to a supporting provider in stable
order. Providers discarded by an ID collision cannot introduce orphan defaults.
Preview validates both modes without writing settings, and apply validates its
candidate before writing any settings files.

## Windows file safety

Backup creation no longer assumes `os.fchmod`, directory descriptors, or
`dir_fd` operations are available on Windows. POSIX still uses 0600 files, 0700
directories, and no-follow descriptor traversal. Windows uses protected DACLs
for the owner and SYSTEM, including inheritable directory permissions.

The Windows metadata scanner opens native handles, rejects reparse points
(including junctions), denies write/delete sharing, and compares opened file
identities with the expected objects. Rollback pins each directory component and
deletes the verified file by handle. Native operation failures remain fatal;
there is no unchecked path traversal fallback. Upload permission failures close
descriptors and remove incomplete sessions.

ZIP validation examines original central-directory names before Python's
platform-specific filename normalization or NUL truncation.

## Regression coverage

- `tests/test_backup_restore_regressions.py`: real configuration export/import,
  origin changes, explicit keys, both restore modes, model defaults, provider ID
  collisions, preview immutability, and upload permission failures.
- `tests/test_windows_backup_files.py`: native ACLs, directory inheritance,
  handle pinning, legacy/sharded metadata, junction rejection, changed root
  identities, failed handle operations, and constrained rollback deletion.
- Existing backup tests verify actual ACLs on Windows and modes on POSIX.
  Link-specific tests explicitly report a skip if the local Windows account
  lacks symlink privilege; junction tests do not need that privilege.
- CI runs the backup, API, and rollback suites on Windows Python 3.11 and 3.13,
  in addition to the existing Linux Python and frontend jobs.

Local Windows Python 3.12 verification: 193 targeted tests completed successfully
with 7 platform/privilege skips. This includes 25 HTTP API tests using real
isolated app instances. WebUI typecheck/build, Ruff, and release-contract checks
passed. No production data, provider credentials, or paid generation calls were
used. The complete Linux and Windows matrix is recorded in the associated PR.

The repository-wide Windows run also exposed existing Unix-only frontend test
launchers, unavailable helper programs, and unrelated POSIX mode/symlink
assumptions. It is not reported as a passing full Windows suite. The relevant
storage and CI-contract assertions were adapted and rechecked (22 tests passed).

Native API references: [CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew),
[SetSecurityInfo](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-setsecurityinfo),
[SetFileInformationByHandle](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle).
