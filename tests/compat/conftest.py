"""The compatibility matrix needs libraries that the other tests do not
(`uv sync --group compat`). Without one of them its row is skipped, unless
`ORMAOS_COMPAT_REQUIRED` is set (the CI job of the matrix sets it)."""

# The examples are programs, not tests of this repository.
collect_ignore_glob = ["cases/*"]
