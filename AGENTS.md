# Repository Guidelines

## Project Structure & Module Organization

This workspace currently contains no application source, tests, assets, or dependency manifests. No application architecture is established yet. The `.agents/`, `.codex/`, `.aws/`, and `.git/` directories are environment metadata; do not use them for application code or assets.

When adding the initial implementation, organize files by responsibility and document the actual layout here. Prefer clear directory names such as `src/`, `tests/`, and `assets/` when appropriate to the chosen stack.

## Build, Test, and Development Commands

No installation, build, development, or test commands are currently configured. Do not assume commands such as `npm test` or `make build` exist.

When introducing tooling, add its manifest and document the exact commands for installing dependencies, running locally, building, and testing. Keep these instructions synchronized with the checked-in configuration.

## Coding Style & Naming Conventions

No language, formatter, or linter has been established. Follow existing conventions once implementation begins. For the initial code, use consistent indentation, descriptive names, and the selected language’s standard conventions. Configure formatting in the repository rather than relying on individual editor settings.

## Testing Guidelines

No testing framework or coverage threshold is configured. Add focused tests alongside new behavior, including important failure cases. Use descriptive test names that identify the behavior under test, and document the test command when introducing the framework.

## Commit & Pull Request Guidelines

Git history is unavailable in this workspace, so no existing commit convention can be verified. Use concise, imperative commit subjects, for example, `Add initial board model`.

Keep pull requests focused. Describe the change, its purpose, and validation performed; link relevant issues and include screenshots for visible interface changes. Explicitly identify checks that could not run.

## Security & Configuration

Never commit credentials, tokens, or local environment secrets. When configuration becomes necessary, document required variables and provide examples containing placeholder values only.
