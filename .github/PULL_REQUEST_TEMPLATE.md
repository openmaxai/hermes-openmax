<!--
  Fill in every section. PRs to `main` (production) only merge after CI passes:
  SAST (Semgrep OSS) + Secret scan (Gitleaks) + Tests, and required approvals.
-->

## What & why

<!-- What does this change do, and why is it needed? Link the issue/ticket. -->

- Closes/Refs:

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor / chore
- [ ] Docs
- [ ] CI / tooling

## How was it tested?

<!-- Commands run, scenarios covered, new/updated tests. -->

- [ ] `uv run pytest -m "not live"` passes locally
- [ ] Added/updated tests for the change (or N/A, explain why)

## Security checklist

- [ ] Semgrep (SAST) passes locally / in CI
- [ ] Gitleaks (secret scan) passes — no secrets, keys, or tokens committed
- [ ] If any scanner finding was suppressed, it is a **confirmed false positive
      approved by the project leader**, using an inline marker
      (`# nosemgrep: <rule-id>` / `# gitleaks:allow`) or an allowlist entry in
      `.gitleaks.toml`, with a short justification. **Never disable a check just
      to make CI green.**

## Reviewer notes

<!-- Anything reviewers should focus on, risks, follow-ups. -->
