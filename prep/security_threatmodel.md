# Threat Model

## Security Boundaries

- Internet → email action endpoint → FastAPI → draft store
- FastAPI → LinkedIn API
- Cron runner → drafts/secrets → LLM CLIs
- Administrators → host, configuration, logs, backups

## 1. Signed Approve/Reject/Edit Links

| STRIDE | Threat | Mitigation |
|---|---|---|
| Spoofing/Tampering | Forged action or `draft_id` | HMAC-SHA256 over the exact canonical encoded payload; reject malformed/duplicate fields; never trust decoded fields before verification. |
| Repudiation | User denies an action | Audit actor, draft, action, token ID/nonce hash, timestamp, and outcome; never log full tokens. |
| Information disclosure | Email scanners, referrers, proxies, or logs leak links | HTTPS only; `Referrer-Policy: no-referrer`; redact query strings; no third-party assets; short TTL. |
| DoS | Token-validation flooding | Per-IP and per-token-prefix rate limits; request-size limits; bounded decoding. |
| Elevation/Tampering | Change `approve` to `edit`, swap draft | Bind `draft_id`, normalized action, expiry, nonce, audience, and token version into MAC. Allowlist actions. |
| Replay | Reuse captured token | Store nonce/token hash; atomically mark consumed with the state transition; single-use across all actions. |
| Timing | Infer valid signatures | Decode to fixed-length bytes and use `hmac.compare_digest`; return uniform errors and similar response timing. |
| Expiry bypass | Overflow, timezone, parsing bugs | Integer Unix timestamp; UTC server time; strict maximum TTL; reject expired, negative, oversized, or non-canonical values. |
| CSRF/Link scanning | Mail scanner triggers GET approval | GET displays confirmation only; state change requires POST. For high-risk approval, require authentication or a second confirmation. |

**Token format hardening:** Prefer `base64url(payload).base64url(HMAC(secret, version || "." || payload))`; define unambiguous length-prefixed fields or canonical JSON—not delimiter parsing without escaping.

## 2. FastAPI Approval Service

| STRIDE | Threat | Mitigation |
|---|---|---|
| Spoofing | Unauthorized API/admin access | Strong authentication, MFA for admins, least-privilege roles, secure sessions. |
| Tampering | Injection or mass assignment | Strict Pydantic schemas with forbidden extra fields; parameterized queries; server-controlled status fields. |
| Repudiation | Missing action history | Append-only audit events with request ID and before/after state. |
| Information disclosure | Stack traces, docs, CORS, response leakage | Disable debug and public docs; restrictive CORS; generic errors; security headers; minimize responses. |
| DoS | Large/slow requests or endpoint floods | Reverse-proxy and application rate limits; body/time/concurrency limits; bounded workers. |
| Elevation | Direct publish or invalid state transition | Explicit state machine; transactional compare-and-set; publish authorization separate from edit authorization. |

- TLS only; trusted-host validation; patched dependencies and pinned lockfiles.
- Do not trust forwarded headers except from an allowlisted reverse proxy.
- Restrict service/database network exposure; run unprivileged with read-only filesystem where possible.

## 3. LinkedIn OAuth Tokens

| STRIDE | Threat | Mitigation |
|---|---|---|
| Spoofing | Stolen token impersonates account | Request minimum scopes; validate OAuth `state`; bind callback to initiating session. |
| Tampering | Token record altered | Authenticated encryption (AES-256-GCM or ChaCha20-Poly1305) with record/account ID as associated data. |
| Repudiation | Untracked refresh/publish | Audit token refresh and publish metadata without token values. |
| Information disclosure | Database, backup, memory, or log exposure | Envelope encryption; key in KMS/secret manager, separate from DB; encrypted backups; strict ACLs; token redaction. |
| DoS | Refresh races invalidate credentials | Per-account refresh lock; atomic token replacement; bounded retries with backoff. |
| Elevation | Excessive OAuth scopes | Least privilege; separate dev/prod apps; revoke tokens when accounts disconnect or compromise is suspected. |

- Never store encryption keys beside ciphertext.
- Rotate wrapping keys and support ciphertext versioning/re-encryption.
- Restrict plaintext token lifetime in memory; never place tokens in CLI arguments or environment dumps.

## 4. Daily Cron + LLM CLIs

| STRIDE | Threat | Mitigation |
|---|---|---|
| Spoofing/Tampering | PATH hijack or malicious CLI/package update | Pin versions and hashes; invoke absolute executable paths; verify provenance/signatures. |
| Tampering/Elevation | Prompt injection causes commands or unauthorized publishing | Treat model output as untrusted data; schema-validate; prohibit tool execution; require approval after final content generation. |
| Repudiation | Pipeline actions cannot be reconstructed | Record job ID, model/version, input/output hashes, approvals, and publish result. |
| Information disclosure | Secrets or private drafts sent to LLM/vendor | Data minimization/redaction; approved providers; disable retention where available; never include OAuth/HMAC secrets. |
| DoS | Hung CLI, retry storm, excessive spend | Timeouts, resource quotas, concurrency lock, capped retries, budget limits, circuit breaker. |
| Elevation | Compromised CLI accesses host | Dedicated unprivileged user/container; minimal filesystem/network access; no shell interpolation; sanitized environment. |

- Prevent overlapping cron runs with an atomic lock.
- Validate generated content length, URLs, encoding, and policy before storing.
- Separate generation credentials from publishing credentials.

# Hardening Checklist

- [ ] HMAC key is randomly generated, high entropy, versioned, and stored in a secret manager.
- [ ] Signatures cover canonical payload bytes, token version, audience, and every authorization-relevant field.
- [ ] Signature decoding is strict and comparison uses constant-time `hmac.compare_digest`.
- [ ] Expiry uses server-side UTC Unix time, strict parsing, bounded TTL, and fail-closed clock-error behavior.
- [ ] Nonces are cryptographically random and atomically consumed with the action.
- [ ] Replayed, malformed, expired, or unknown-version tokens are rejected uniformly.
- [ ] GET requests never approve, reject, edit, or publish.
- [ ] Rate limits exist per IP, account, endpoint, and token/nonce; repeated failures trigger backoff.
- [ ] OAuth tokens use authenticated envelope encryption; KMS keys are separate from data and backups.
- [ ] Secrets never appear in source, images, CLI arguments, prompts, logs, traces, metrics, or error responses.
- [ ] Secrets and OAuth tokens have rotation/revocation procedures with tested recovery.
- [ ] Logs redact authorization headers, cookies, URLs/query strings, HMAC tokens, OAuth tokens, prompts, and generated sensitive content.
- [ ] Audit logs retain nonce/token hashes—not raw credentials—and are access-controlled and tamper-evident.
- [ ] Database transitions use transactions and compare-and-set to prevent double approval/publish.
- [ ] Publishing fails closed unless the exact immutable content revision has a valid, unconsumed approval.
- [ ] Editing invalidates prior approvals; regenerated content requires new approval.
- [ ] Ambiguous state, validation failure, timeout, LinkedIn error, or audit-write failure prevents publishing.
- [ ] Idempotency keys prevent duplicate LinkedIn posts during retries.
- [ ] Admin access uses MFA, least privilege, and network restrictions.
- [ ] Dependencies, host, FastAPI server, reverse proxy, and LLM CLIs are pinned, scanned, and patched.
- [ ] Incident procedures cover key rotation, OAuth revocation, token invalidation, publish suspension, and audit review.
