# opynrefine

Python 3.x client and CLI for working with the [OpenRefine API](https://openrefine.org/docs/technical-reference/openrefine-api).

## Getting Started

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[test]
```

### CLI usage

```bash
opynrefine --base-url http://127.0.0.1:3333 list-projects
opynrefine call core/get-models --params '{"project":"123"}'
opynrefine create-project "New dataset" data.csv --format-hint text/line-based/*sv --options '{"separator":","}'
```

## Tests

Pytest-based tests include unit coverage and an integration test that exercises a running OpenRefine instance on `http://127.0.0.1:3333`. If OpenRefine is not running locally the integration test is skipped automatically.

```bash
pytest
```
