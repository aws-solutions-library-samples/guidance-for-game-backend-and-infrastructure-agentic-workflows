# Public Content Rules

This repository, its issues, pull requests, examples, test fixtures, and
published logs are public. Review content before publishing it.

## Prohibited Content

Do not publish:

- credentials, private keys, tokens, cookies, or authentication headers;
- customer data, personal data, private conversations, or unredacted logs;
- real AWS account IDs, resource IDs, URLs, or deployment details;
- internal domains, package names, issue links, project names, or source
  attribution; or
- non-public architecture, incident, operational, or security information.

Report suspected vulnerabilities through the process in
[`SECURITY.md`](../SECURITY.md), not through a public issue.

## Synthetic Examples

Examples and tests must use clearly synthetic data:

- AWS account ID: `123456789012`
- email and web domains: `example.com`, `example.net`, or `example.org`
- resource names: product-oriented names such as `game-agent-demo`
- identifiers: fixed test values that cannot be confused with deployed
  resources

Construct the smallest synthetic log or response needed by a test. Do not copy
production output and redact it after the fact.

## Automated Check

Run the repository-wide check from the repository root:

```bash
python3 scripts/check_public_content.py
```

The check rejects high-confidence credential forms, non-synthetic account IDs
and email addresses, and known internal domains. CI scans every tracked text
file. Pre-commit scans staged text files.

Maintainers can keep private project names outside the repository in a file
containing one term per line, then include that file in a local scan:

```bash
python3 scripts/check_public_content.py \
  --denylist-file /path/outside/repository/private-terms.txt
```

The scanner does not print the matching value. The denylist must remain
untracked and outside the repository.

Automation cannot reliably identify every private name, source attribution,
customer detail, or sensitive design statement. Review those manually before
publishing. When uncertain, stop and replace the content with a synthetic,
product-only description.
