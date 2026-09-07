# Phase2.2s worker-isolated Codex state

Child Codex workers use the `worker-isolated-sqlite-v1` policy. The Python
parent creates one unique worker artifact directory before each process starts:

`<run>/<companies>/<company-order-and-ticker>/workers/<worker-id>`

The path components come from the acquired assignment and worker plan. They
must be short single path components, remain below the repository, and must
not already exist. The worker directory itself is `sqlite_home`; no additional
nested state directory is created. A collision or initialization failure stops
before the child process starts.

Each child runs with `codex exec --ephemeral` and inline settings for its
directory (`sqlite_home`) and `history.persistence=none`. `CODEX_HOME` remains
unchanged. Authentication files are not copied into the isolated directory.
The shared global SQLite database is not selected for worker runtime state.

The run manifest freezes this policy. A mismatch fails before assignment
acquisition. JSONL lifecycle events and exact completed-turn usage continue
to be retained in the worker telemetry.

A controlled smoke may pass an already-created empty worker directory through
the same stage executor. The normal runner always creates its worker directory
exclusively and never reuses an existing directory.
