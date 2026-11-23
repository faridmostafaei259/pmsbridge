# Shared test helpers

Modules here are importable from every test (`pythonpath` in `pyproject.toml`), for example
`from mcp_helpers import ...`. Put helpers used by more than one test folder here instead of
changing `sys.path` in a test. Module names must be unique and must not be `conftest`.
