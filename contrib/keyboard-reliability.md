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
pending keyboard transitions. Logging and state publication after the USB syscall
also run outside that lock. A blocked reporting sink can still delay the worker,
including all-release delivery, but cannot keep the parent's reset-generation
update waiting for the write lock. Other HID devices retain their existing output
policy.

Each keyboard LED drain handles at most 64 reports, checks stop/reset between
reads, and ends on read errors or EOF. A continuously readable endpoint cannot
keep the worker inside one drain indefinitely. These are iteration bounds, not
elapsed-time guarantees for logging, callbacks, or device operations.

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
The write checkpoint pauses the raw USB syscall rather than the reporting helper.
USB hardware remains mocked; no test sends keys to the target.

The 38 cases cover healthy delivery, failed presses, EAGAIN/ESHUTDOWN, device
readiness, held keys, overlapping keys and modifiers, multiple reports from a
single event, retry exhaustion, explicit recovery, stop requests, reset
cancellation, late old-generation events, release after USB reconnect, clear
during LED reads, and transient or persistent LED processing errors.
Low-level cases exercise the real LED reader with continuously ready input,
read errors, EOF, stop/reset cancellation, and ordered press/release delivery.
They also verify that blocked logging or state publication does not block reset.
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

## Active Hardware Trial

On 2026-10-06 the user authorized a targeted SSH trial of revision `638c3a0c`
against a Windows 11 target with a physical keyboard available. This is a custom
Python patch, not a firmware upgrade or a vendor-supported installer.

The complete installed `device.pyc` and `keyboard.pyc` matched the protected
baseline and the bytecode compiled from pre-patch revision `3e8dd23c`, ignoring
only source filenames and line locations. The patched modules were compiled with
the device's Python 3.12.5 and imported against its installed dependencies in a
separate staging process. Pure report checks did not open USB devices.

[The deployment tool](keyboard_trial.py) saved exact originals and a manifest,
stopped KVMD, replaced only those two bytecode files, preserved their recorded
ownership/modes/mtimes and directory metadata, then started KVMD. No source
overrides or Python caches were installed. No configuration, firmware, or boot
files were changed; there was no reboot or synthetic target input.

The service script printed an immediate stop failure while the old daemon was
exiting. The tool independently waited until all old KVMD/HID workers were gone
before replacement. Post-install checks verified both patch hashes and metadata,
one new main process and the expected keyboard/mouse/touch workers, a responsive
daemon socket, and HTTP 200 from the LAN web interface. Video and target input
remain manual acceptance checks, not established by these health checks.

Private trial directory: `/userdata/glkvm-trials/keyboard-638c3a0c/`.
Its originals, manifest, and rollback tool also have a protected off-device copy
under the baseline backup's `trial-638c3a0c` directory. Seven filesystem tests
verify exact install/rollback, fail-closed hash checks, and stop/copy/start failure
handling without services or hardware. Actual running-service rollback has not
yet been exercised; it is required after the user's first trial.

Restore the original modules from the manager PC with:

```powershell
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes root@glkvm /userdata/glkvm-trials/keyboard-638c3a0c/rollback.sh
```

[The rollback wrapper](rollback-keyboard-trial.sh) invokes the same tool over SSH,
not through the web UI. It checks the private originals, stops KVMD and waits for
its workers to exit, atomically restores both original bytecode files and recorded
metadata, checks their hashes, then starts KVMD and checks daemon/worker readiness.
Do not upgrade firmware or edit the affected package files while this trial is
active. This command requires functioning SSH and device Python; it is not a
recovery mechanism for firmware or storage damage.

Use one LAN browser session and a disposable Notepad document on the target.
Compare normal typing, long holds and release, Shift combinations, overlapping
keys, and Backspace. Stop on unpredictable input and use the physical keyboard
as needed. After testing, restore the original software even if typing succeeds,
then verify web access, video, and keyboard behavior before retaining the patch.

## Free-Form Diagnostic Capture

The capture does not require prescribed text, key order, or typing speed. Type
naturally in the existing KVM tab and reproduce the actual failure, including
overlaps, modifiers, holds, corrections, and navigation keys. Use a disposable
target document. Logs include physical key codes and literal browser key values;
do not type passwords or other secrets while either recorder is active. Keep
captured JSON, trace files, and analysis output private and outside Git.

Run the updated [browser snippet](capture-keyboard-browser.js) in DevTools in the
existing live KVM tab, without reloading or opening another KVM session. Updating
an inactive recorder retains its previous API and data as
`kvmKeyboardCapturePrevious`; updating an active recorder is rejected.
The prepared message and exported JSON must both identify version 3. Selecting
the updated file in the editor does not update the recorder already in the tab.

After the manager confirms the KVM trace is attached, start browser recording:

```javascript
kvmKeyboardCapture.start();
```

The browser defaults to five minutes and 50,000 records. An optional integer
argument sets a duration from one to 600 seconds. Key events retain order,
timestamps, modifiers, repeat flags, key values, legacy key codes, and target
element metadata. Composition and input events retain flags and data length,
not composed text. `status()` reports counts
and whether any WebRTC sends were observed. A lack of send records is not proof
that the frontend failed to send; cached send methods, workers, or other browser
realms can escape the hooks.

When the fault occurs, stop and export the browser log:

```javascript
kvmKeyboardCapture.stop();
kvmKeyboardCapture.download();
```

The [KVM recorder](capture_keyboard_trial.py) defaults to six minutes, providing
setup margin around the browser session. Its `start --seconds 360` command
discovers the keyboard worker and stores a descriptor-filtered `strace` in a
private trial subdirectory. `stop --directory <capture-directory>` signals only
the trace supervisor, which detaches the tracer without releasing keys or
restarting KVMD. Recording remains bounded even if the SSH connection closes.
Neither recorder changes normal key-hold or repeat behavior.

The manager can compare arbitrary captured input offline:

```text
python contrib/analyze_keyboard_capture.py --browser <browser.json> --trace <trace.txt> --metadata <metadata.json>
```

[The analyzer](analyze_keyboard_capture.py) uses the repository CSV key map and
decodes queue pickle opcodes without importing or executing pickle payloads. It
compares browser discrete transitions with worker events, then reconstructs
ordered USB state reports from those worker events. The JSON result identifies
missing, extra, or reordered transitions, failed writes, report mismatches, hold
duration, overlap, and matched event-to-write timing. Browser auto-repeat
keydowns are not counted as new discrete presses. Recorded clear/reset events
and unsupported browser codes are surfaced separately.

Clock alignment is estimated from matching transitions and includes clock skew
and transport delay; it is not a one-way latency measurement. Events outside
the shared recording window are excluded and reported, not labeled dropped.
Incomplete parsing is explicitly flagged, and reconstruction assumes the KVM
capture began with no held keys. USB writes still do not establish receipt by
Windows or the final text after application editing, layout mapping, or IME
processing. Preserve the target result as well; a target-side event capture may
be needed if both comparisons match despite a visible failure.

### Unicode Packet Input

An empty browser `code` with `keyCode: 231` is Windows `VK_PACKET`, used to
deliver Unicode characters rather than a physical scan-code key event. It is
not the IME process key (`229`). Check the `key`, composition flags, and nearby
press/release pairs instead of assuming transport loss or an IME failure.

PowerToys Quick Accent is one possible upstream source: it intercepts a held
letter followed by its activation key, which can include Space, then inserts
text. Ordinary overlapping typing can trigger this path. A paired failure
capture showed packet Space events, missing browser releases, and matching
worker-to-USB reports, with Quick Accent running and no excluded apps. This
makes Quick Accent a candidate, not a confirmed cause until an A/B test passes.
Temporarily disable only that utility, with permission, and repeat the same
free-form capture without changing the KVM or other input settings. Do not
compensate by timing out held keys or releasing them on every Space press.

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
