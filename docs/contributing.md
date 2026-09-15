# Contributing

DanuBaaS keeps code, deployment, API contract, and documentation in one
repository. Start with the [repository contribution guide](https://github.com/AndreasWillibaldWeber/DanuBaaS/blob/main/CONTRIBUTING.md).
A change description should state the problem, resulting behavior, verification,
and any migration or operational impact.

## Edit the documentation

Pages live in `docs/`, and their order is set in `mkdocs.yml`. The icon, banner,
and site styles live in `docs/assets/`. Install the pinned site dependencies in a
virtual environment and preview locally:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r docs-requirements.txt
python -m mkdocs serve
```

Before opening a pull request, run:

```sh
python -m mkdocs build --strict
```

The Documentation workflow builds every pull request and publishes a successful
`main` build to GitHub Pages. Pages must be configured with **GitHub Actions** as
its publishing source in repository Settings → Pages. A failed strict build blocks
publication. Edit links should take readers back to the corresponding source file.

For public API changes, update `docs/openapi.yaml`, the relevant guide, and
`README.md` in the same change. For route or storage semantics, include evidence
for retries, batch rollback, and concurrency. Follow the
[quality strategy](quality.md) when describing what has and has not been verified.
