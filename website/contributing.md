# Contributing

We'd love your help expanding the scanner ecosystem.

## Contributing a New Scanner

1. **Fork the repo** and create a branch for your scanner
2. **Create your scanner directory:** `community/<your-scanner>/`
3. **Write `adapter.py`** with a class that implements `name`, `poll()`, and `configure()`
4. **Write `teammate.json`** with your scanner's [manifest](/build-your-own/manifest)
5. **Add the [sandboxed execution block](/build-your-own/sandboxed-execution#entry-point-boilerplate)** at the bottom of your adapter
6. **[Test locally](/build-your-own/testing)** using the stdin/stdout protocol
7. **Submit a PR** with a description of what your scanner monitors

## Guidelines

- Keep your scanner self-contained — use only Python stdlib when possible
- List any required CLI tools in `requirements.cli_tools`
- Never hardcode secrets — use `token_env` to reference environment variables
- Handle errors gracefully — return `([], watermark)` on failure so the watermark doesn't advance
- Include a descriptive `teammate.json` so users know what they're installing

## Scanner Ideas (Contributions Welcome)

We're actively looking for community scanners for:

- **Datadog** — monitor alerts and anomaly detection
- **Opsgenie** — alert management
- **Custom internal tools** — if it has an API, it can be a scanner

## License

HiveScanner is licensed under **Apache 2.0 + Commons Clause**. By contributing, you agree that your contributions will be licensed under the same terms.
