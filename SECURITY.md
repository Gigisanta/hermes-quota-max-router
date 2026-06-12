# Security Policy

## Supported Versions

The router is currently in **0.2.x** (pre-1.0). Security fixes are made on
`main` and shipped as fast-follow patch releases.

| Version | Supported          |
|---------|--------------------|
| 0.2.x   | :white_check_mark: |
| < 0.2   | :x:                |

We strongly recommend running the latest commit on `main` in production. The
project does not yet maintain stable LTS branches.

---

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security problems.**

Send a private report to **giolivosantarelli@gmail.com** with the subject
prefix `[SECURITY] hermes-quota-max-router`. The body should include:

- A clear description of the issue and the attack surface.
- A reproducer (code, curl, screenshot, or steps).
- The commit / version you observed it on.
- Your assessment of impact and severity (optional — we'll triage).

We will acknowledge receipt within **48 hours** and provide an initial
assessment within **5 business days**.

### What to expect

| Severity | First response | Target fix window |
|---|---|---|
| Critical (RCE, auth bypass, secret leak) | < 24 h | < 7 days |
| High (DoS, data exposure) | < 48 h | < 30 days |
| Medium / Low | < 5 business days | Next minor release |

Critical fixes are released as a new patch version and a GitHub Security
Advisory is published. We follow [coordinated disclosure][cd] and ask
reporters to give us a reasonable window (default: 90 days) before public
disclosure.

[cd]: https://en.wikipedia.org/wiki/Coordinated_vulnerability_disclosure

---

## Threat model summary

The router is a **single-tenant OpenAI-compatible proxy**. Its trust
boundary is:

```
trusted: server/app.py process + operator's .env file
untrusted: incoming HTTP requests (chat completions, admin endpoints)
semi-trusted: the catalog feeds (OpenRouter, Hugging Face) — they list
              models but no LLM traffic flows through them
```

### What the router does NOT protect against

- **Compromise of an upstream LLM provider.** If OpenRouter / DeepSeek /
  Gemini returns malicious content, the router forwards it. Use output
  filtering at the application layer.
- **Prompt injection in user messages.** A malicious user message can
  manipulate the orchestrator's task analysis. The router does not sandbox
  tool execution; treat tool results as untrusted.
- **Compromise of the host OS.** The router runs as a normal Python process.
  Standard OS hardening applies.

### What the router DOES protect against

- **Unauthenticated access** when `ROUTER_MASTER_KEY` is set (server/app.py).
- **Per-client rate limit bursts** (60 req/min/IP default).
- **Common HTTP response headers** (HSTS, CSP, X-Frame-Options, etc.).
- **Secret leakage in git history** — see `gitleaks` CI and the pre-commit
  hook below.

---

## Secrets hygiene

The recent commit history contains a placeholder string
(`__REDACTED_GEMINI_KEY__`) in `scripts/run_live_server.py` and
`scripts/run_router_live.py`. **No real key was ever committed** — the
placeholders were a templating artefact from the project bootstrap. Still,
as a precaution:

1. **Rotate** your Gemini key at <https://aistudio.google.com/apikey>
   before deploying.
2. **Never commit** `.env` (it is gitignored).
3. **Use the pre-commit hook** below to catch secrets in new commits.

### Pre-commit hook (recommended)

```bash
pip install pre-commit
cat > .pre-commit-config.yaml <<'EOF'
repos:
  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.18.4
    hooks:
      - id: gitleaks
EOF
pre-commit install
```

This is also enforced in CI by the `lint` workflow.

---

## Cryptography

- **No custom crypto.** The router does not implement encryption, signing,
  or key exchange.
- **TLS** is terminated by the reverse proxy (nginx, Caddy, Cloudflare) in
  front of the router. Set `Strict-Transport-Security` at the proxy, not
  on the router.
- **API key comparison** uses `hmac.compare_digest` (constant-time) — see
  `core/security.py:get_master_key()`.

---

## Dependency security

We pin dependency floors (`>=`) in `pyproject.toml`. To check for known
vulnerabilities:

```bash
pip install pip-audit
pip-audit
```

This is run periodically by the maintainer, not on every commit.

---

## Security-related changelog highlights

- **iter 15** — `if master_key:` no longer allows silent unauthenticated
  access. Server now refuses to start without `ROUTER_MASTER_KEY` unless
  `ROUTER_ALLOW_INSECURE_NO_AUTH=1` is explicitly set.
- **iter 15** — HSTS, CSP, Permissions-Policy headers added.
- **iter 15** — `RouterCallResult.to_dict()` preserves `tool_calls` (was
  silently dropping them from the JSONL audit trail).

For the full history, see [CHANGELOG.md](./CHANGELOG.md).
