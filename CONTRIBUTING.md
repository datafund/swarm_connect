# Contributing to Provenance Gateway

Thanks for your interest in contributing! Here's how to get started.

## Getting Started

1. Fork the repository
2. Clone your fork and create a branch from `dev`:
   ```bash
   git checkout dev
   git pull origin dev
   git checkout -b feature/my-feature
   ```
3. Install dependencies:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   ```

## Development Workflow

### Branching

```
feature branches → dev → main
```

- **Never push directly to `main` or `dev`** — always use pull requests
- Create feature branches from `dev`
- PRs to `dev` deploy automatically to staging
- PRs from `dev` to `main` deploy to production

### Running Tests

```bash
source venv/bin/activate
python -m pytest tests/ -v
```

All tests must pass before submitting a PR.

### Running Locally

```bash
python run.py
# Gateway available at http://localhost:8000
```

## Pull Request Guidelines

- Keep PRs focused — one feature or fix per PR
- Write clear commit messages describing the "why"
- Add tests for new functionality
- Update documentation if you change API behavior
- Ensure all existing tests still pass

## Reporting Issues

- Use [GitHub Issues](https://github.com/datafund/swarm_connect/issues)
- Check existing issues before creating a new one
- Use the provided issue templates when available
- Include steps to reproduce for bug reports

## Code Style

- Follow existing patterns in the codebase
- Use type hints for function signatures
- Keep functions focused and reasonably sized
- Use `logging` instead of `print` statements

## Questions?

Open a [discussion](https://github.com/datafund/swarm_connect/issues) or reach out to the team.
