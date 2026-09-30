# NY market migration postcheck

The 2026-09-26 code and Git cutover completed with `business_restart: false`.
Its completion record reported `ny: PAUSED`; the automation's new cwd was correct,
but no restart gate checked whether the morning report had resumed.

After a future code, environment, or project migration, run:

```powershell
python tools/check_ny_automation_state.py
```

When the migration changes the project identity or code location, pass the
new values with `--expected-project` and `--expected-cwd`.

The read-only check exits nonzero unless **only the `ny` automation** is ACTIVE,
scheduled daily at 07:00 Asia/Tokyo, local, and attached to the expected runtime
directory and project. Record its JSON result in migration evidence. If it fails,
resolve `ny` explicitly and rerun the check before calling the migration complete.
Do not resume other paused automations as a group.

At the next scheduled 07:00 run, also check report/run/API read-back and the
Viewer page. ACTIVE and the configuration check alone do not prove publication.
