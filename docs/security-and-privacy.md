# Sunday Voice – Security and Privacy

## Roles and Access

- Admin:
  - Manages operators and system configuration.
  - Can see all sessions and high-level stats.
- Operator:
  - Can create and manage sessions.
  - Access only to sessions in their stake instance.
- Listener:
  - Anonymous, read-only access to translation streams.

## Authentication

- Local user accounts with hashed passwords.
- Session or token-based auth for operator/admin UI.
- Strong password policy recommended (configurable guidelines).

## Authorization

- RBAC enforced at API layer:
  - Admin-only endpoints for provider config and system settings.
  - Operator endpoints for session management.
  - Public endpoints for join/view, but read-only and scoped to session.

## Public Links and Abuse Controls

- Session join links:
  - Include non-guessable identifiers.
  - Expire with session.
- Numeric codes:
  - Shorter and easier to type, but bound to time-limited sessions.
- Rate limiting:
  - Join attempts per IP.
  - WebSocket connection attempts.
- Abuse guardrails:
  - No write endpoints available to anonymous users.
  - Session isolation so one session cannot read another’s data.

## Privacy

- Content scope:
  - Only standard public-style meetings (sacrament, stake conferences, classes).
  - No private or confidential meetings in MVP.
- Retention:
  - Audio content buffered briefly; not stored long-term.
  - Transcripts and translations retained for up to 48 hours for troubleshooting.
  - After 48 hours, delete content but keep aggregated stats.
- Provider usage:
  - Whisper API and translation providers may receive audio/text.
  - Configuration and docs must clearly state data usage and training policies of providers.

## Audit Logging

- Record:
  - Logins, failed logins.
  - Session creation/start/stop.
  - Provider configuration changes.
  - Retention and cleanup operations.
- Retain audit logs longer-term as they are metadata, not transcript content.

## Encryption

- TLS required for all external access.
- Internal secrets management (API keys) via environment variables or secure config files.
