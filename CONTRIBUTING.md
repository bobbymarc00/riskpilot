# Contributing

Keep changes focused, offline-tested, and fail-closed.

- Preserve legacy ledger schemas, payloads, callbacks, environment variables, state paths, skill path, and systemd unit names.
- Use Decimal for financial values and transactions for mutations.
- Mock market/notification paths; never add real write credentials.
- Run `./scripts/verify.sh`, compilation, shell syntax checks, and `git diff --check`.
- Keep public examples free of personal IDs, credentials, absolute home paths, and fake submission links.
- Never commit runtime config, databases, logs, OAuth caches, credentials, or backups.
- Do not add LIVE execution without verified protected Spot schema, native confirmation binding, reconciliation, and mandatory protection.
