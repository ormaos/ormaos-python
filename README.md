# ormaos

Ormaos records what happened inside a Python execution and hands it to the developer and to their coding agent.

> Status: being built for Hack Brasil 2026. Nothing is recorded yet; only the package skeleton and `ormaos --version` exist.

## Requirements

Python 3.12 or newer. The `ormaos` package uses only the standard library. Nothing leaves your machine: no AI, no network, no telemetry.

## Development

```bash
uv sync
uv run ormaos --version
uv run ruff format . && uv run ruff check .
uv run mypy --strict src
uv run pytest
```

## License

Apache 2.0. See [LICENSE](LICENSE).
