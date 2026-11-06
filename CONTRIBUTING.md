# Contributing

This project is an engineering preview. Please describe the issue and expected behavior before proposing large changes.

## Development

Use Python 3.12, uv and Node.js 20+. Follow the setup instructions in [README](README.md). Run `uv sync --extra dev`; for the dashboard run `npm ci` inside `web`.

For Python changes, run the relevant pytest tests and the repository's Ruff and mypy checks. For dashboard changes, run `npm run typecheck`, `npm run lint`, `npm test` and `npm run build` inside `web`. The exact CI commands are in [.github/workflows/ci.yml](.github/workflows/ci.yml). Explain what you verified and any remaining limitations in the pull request.

Tests should use fake providers by default. Live model requests incur costs and require explicit credentials and a budget. Docker and external-service checks require their respective local setup; never describe a skipped check as passed.

## Submission hygiene

Do not include `.env`, credentials, local logs, runtime databases, private code, or model request/response payloads. Check the complete diff and new files before submitting. Keep third-party copyright and license notices and document the origin and redistribution rights of added code, fixtures, datasets and images.

Project-owned code is licensed under [Apache License 2.0](LICENSE). Unless explicitly stated otherwise, contributions intentionally submitted for inclusion are provided under that license, as described in its Section 5. Submit only material you have the right to contribute under those terms. Third-party material retains its applicable license and notices. No contributor license agreement is currently required.
