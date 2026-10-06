# Keyboard Reliability: LAN Trial Plan

## Scope

Fix missed or stuck keys on a direct LAN connection first. Preserve ordinary
key-down/key-up events, modifiers, shortcuts, long holds, and target-side repeat.
The immediate-release workaround is not the intended solution.

The first change is confined to keyboard USB report delivery. It does not change
the network protocol or the existing mouse report policy.

## Evidence and Behavior

The original [writer](../kvmd/plugins/hid/otg/device.py) could replace a failed
keyboard report with a report from a later event. A failed press followed by a
successful release could therefore lose the press despite ordered network input.
This was reproduced against the actual production loop with mocked USB writes.
Local inspection of the backed-up deployed bytecode found the same control flow.
That establishes a failure mechanism, not proof that it caused every observed typo.

The [keyboard process](../kvmd/plugins/hid/otg/keyboard.py) now enables ordered
delivery: each report must succeed before the next report is processed. Temporary
USB backpressure does not turn a normal press into an immediate-release tap.

Clear/reset advances a shared generation. Queued events and pending retries from
older generations are cancelled. The final generation check and nonblocking USB
write share the reset lock: a reset waits for an already-started write, and no
old-generation write begins after the reset advances the generation. LED reads
run outside that lock; their processing errors are logged without discarding
pending keyboard transitions. Other HID devices retain their existing behavior.

If the configured write attempts are exhausted,
the worker clears its local key state, attempts an all-release report when USB
permits, reports HID offline, and rejects ordinary events until clear/reset.
Recovery must successfully write its clear report before later input proceeds.

Retry count bounds attempts, not a fixed elapsed timeout: USB readiness checks
also consume time. Existing retry settings still need validation on real hardware.

## Local Validation

[The focused tests](../testenv/tests/plugins/hid/test_otg_keyboard.py) call the
production keyboard worker directly in the test process for most cases. Two
additional Linux fork cases exercise real worker processes, multiprocessing
queues, and the shared generation lock with explicit read/write/reset handshakes.
USB hardware remains mocked; no test sends keys to the target.

The 24 cases cover healthy delivery, failed presses, EAGAIN/ESHUTDOWN, device
readiness, held keys, overlapping keys and modifiers, multiple reports from a
single event, retry exhaustion, explicit recovery, stop requests, reset
cancellation, late old-generation events, release after USB reconnect, clear
during LED reads, and transient or persistent LED processing errors.
One case verifies all 100,000 discrete transitions with intermittent write failures.
The two cross-process cases also passed 20 repetitions (40 cases total), without
deadlocks or ordering failures observed. These are software checks, not hardware
or end-to-end network acceptance.

Run the focused suite in a Linux Python environment with KVMD test dependencies:

```sh
python -m pytest -q -p no:cacheprovider testenv/tests/plugins/hid/test_otg_keyboard.py
```

The repository's standard container test entry point is `make tox E=pytest`.
Windows verification used a local Linux development image, `glkvm-hid-tests:local`,
with the checkout mounted read-only. That image is not a firmware upgrade image.

Focused tests pass. New tests and the keyboard module pass the repository's
flake8 configuration. The shared writer has pre-existing W291/E128 warnings;
lint passes when only those baseline warning categories are excluded.

## Inspected Device and Backup

Read-only inspection on 2026-10-06 established:

- Model RM10; `/etc/version` reports `V1.10.1 release2`.
- ARM64 Buildroot with kernel 6.1.141; its OS build label differs from the UI label.
- KVMD is installed as sourceless Python bytecode under
  `/usr/lib/python3.12/site-packages/kvmd`.
- KVMD uses `/etc/init.d/S98kvmd`, not systemd.

Baseline ID: `20261006T184701Z-4d7013e2bc99`.

Protected copies of `baseline.tar` and verification manifests are stored under:

- Device: `/userdata/glkvm-backups/20261006T184701Z-4d7013e2bc99/`.
- Manager: `C:\Users\richa\glkvm-backups\20261006T184701Z-4d7013e2bc99\`.

Archive SHA-256:

```text
20c2a81e89cde67c83c46b4f577d4ddc39742e1774990568f9e8eff40c254923
```

The archive is 2,527,232 bytes and contains 284 regular files and 63 directories.
It includes installed KVMD, `/etc/kvmd`, version labels, and the KVMD, gl-pion,
and OTG service scripts. Configuration may contain secrets: keep the archive
private, outside Git, and do not publish file contents in diagnostic logs.

Device-side staging extraction matched regular-file hashes, numeric ownership,
modes, sizes, and timestamps. BusyBox extraction needed a directory-mtime
supplement, applied only inside staging. The off-device copy matched the archive
hash and payload/metadata manifests. Baseline files were stable across capture.

This is a verified file-level restore, not a successful service rollback test or
a firmware/boot backup. ACLs, extended attributes, atime, and ctime are not covered.

## Deployment and Rollback Gate

No deployment or service restart is authorized by this document. Before an
explicitly approved trial:

1. Recheck that the installed affected files still match the recorded baseline;
   if they changed, take and verify a new baseline.
2. Compare deployed bytecode and local changes for compatibility, including the
   device's Python version and all imports. Do not replace the whole package
   merely because the checkout imports successfully in a development container.
3. Stage only the required keyboard patch files outside production. Record every
   new or replaced source/bytecode file, its original metadata, and its hash.
4. Prepare an exact restore procedure from the protected baseline. Rollback must
   remove newly introduced source files and cached bytecode, restore replaced
   files and metadata, and use the device's verified service mechanism.
5. Obtain approval for the specific patch, restart, disposable target window, and
   rollback procedure before changing installed files or sending synthetic input.
6. Check SSH, web access, video, and ordinary keyboard behavior after deployment.
   Exercise rollback and verify those checks again before claiming it is proven.

Do not use the current [copy script](../apply_to_glkvm.sh) unchanged: it deletes
the installed package before copying replacements and provides no backup gate.
The local-upgrade UI expects a valid firmware image, not arbitrary source code.
Do not flash firmware, bypass signatures, modify boot partitions, or reboot the
device merely to try this Python fix.

## Remaining Work

- Confirm the active frontend/Native WebRTC relay path and correlate LAN failures
  with USB readiness and write errors without logging real typed content.
- Verify browser capture, multiprocessing behavior, and target-side events on
  hardware in an agreed disposable window. Successful USB writes alone do not
  establish what a target application consumed.
- Resolve shared-session clearing: another tab or relay connection must not clear
  a healthy controller's pending input. This change does not alter that behavior.
- Define bounded queue/backpressure and visible suspension/resumption UX. Current
  suspension uses HID online state and logs; the shipped frontend's presentation
  remains unverified. This is not yet an end-to-end acknowledged input protocol.
- After LAN acceptance, test direct and relayed Tailscale paths, international
  latency/jitter, stalls, and watchdog limits. Do not gate each keystroke on a
  round-trip acknowledgement or replay uncertain input after a target change.

Within the supported operating conditions, acceptance requires no missing,
duplicated, or reordered discrete transitions. During sustained outages, input
must be explicitly suspended, held keys released as soon as USB permits, and
uncertain stale input not replayed. Normal long holds must remain functional.
