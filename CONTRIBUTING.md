# Contributing

Thanks for your interest in the Proxmox Kubernetes operator.

## Development setup

```bash
poetry config virtualenvs.in-project true
poetry env use python3.12
make poetry-install
make pre-commit-install
```

## Before opening a pull request

```bash
make test
make chart-security   # requires helm and kubescape in PATH
```

## Code style

- Python 3.12, type hints where practical
- Formatting and linting: Ruff (`pyproject.toml`)
- Logging prefixes for API and firewall operations: `[API CALL]`, `[FIREWALL CREATE]`, `[FIREWALL CLEANUP]`
- Do not commit secrets, Proxmox tokens, or environment-specific registry credentials

## Pull requests

- Keep changes focused; describe the problem and the approach in the PR
- Ensure tests pass
- Update docs or samples when behavior or CRDs change

## License

By contributing, you agree that your contributions are licensed under the [Apache License 2.0](LICENSE).
