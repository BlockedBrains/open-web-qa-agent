# Contributing

## Before you start

- Read `README.md` for setup and project structure.
- Keep secrets, saved sessions, screenshots, and crawl artifacts out of commits.
- Use `sites/example-site/` as the template for any sample or test-safe profile you want to share.

## Local setup

1. Create a virtual environment and install dependencies from `requirements.txt`.
2. Install the Playwright browser with `playwright install chromium`.
3. Copy `.env.example` to `.env` and add your local credentials.
4. Copy `sites/example-site/` to a private local site profile and update its `site.json`.

## Development workflow

1. Make focused changes.
2. Run the smallest verification step that proves the change works.
3. Update docs when behavior, setup, or artifacts change.
4. Keep generated outputs such as `qa_data.js`, `crawl_state.json`, screenshots, and session files out of commits.

## Pull requests

- Describe the user-facing or developer-facing impact.
- Note any setup changes, migrations, or environment variable changes.
- Include verification steps you ran.
- Include screenshots only when they help explain UI or report changes.

## Style expectations

- Prefer small, explicit changes over broad rewrites.
- Preserve the separation between explorer mode and workflow mode.
- Keep site-specific secrets and private targets out of committed files.

## Reporting bugs

Open an issue with:

- what you were trying to do
- how to reproduce it
- expected behavior
- actual behavior
- relevant logs or report excerpts with secrets redacted

For security issues, do not open a public issue. Follow `.github/SECURITY.md`.
