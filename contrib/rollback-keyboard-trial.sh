#!/bin/sh
set -eu

TRIAL=/userdata/glkvm-trials/keyboard-638c3a0c
BACKUP=/userdata/glkvm-backups/20261006T184701Z-4d7013e2bc99/baseline.tar

exec /usr/bin/python3 -B "$TRIAL/keyboard_trial.py" \
    "$TRIAL/base.zip" "$BACKUP" --action rollback
