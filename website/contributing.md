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
- Keep `requirements.cli_tools` empty; community scanners cannot depend on child CLI tools in the constrained runtime
- Never hardcode secrets — use `token_env` to reference environment variables
- Validate config/provider/state strictly, bound requests and output, and return `([], watermark)` on incomplete or ambiguous failure
- Add focused tests for first-run silence, equal timestamps, pagination exhaustion, rate limits, malformed late pages, redirects, and prompt-like provider text
- Include a descriptive `teammate.json` so users know what they're installing

## Scanner Ideas (Contributions Welcome)

We're actively looking for community scanners for:

- **Datadog** — monitor alerts and anomaly detection
- **Opsgenie** — alert management
- **Custom internal tools** — if it has an API, it can be a scanner

## License

HiveScanner is licensed under **Apache 2.0 + Commons Clause**. By contributing, you agree that your contributions will be licensed under the same terms.
