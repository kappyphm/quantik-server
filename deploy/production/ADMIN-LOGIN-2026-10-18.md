# Admin login incident and verified production repair

Requests `2b92f2ec-072f-422a-82c2-4047306c6884` and
`e5f47d90-28b9-4660-8d30-dba5574e167f` returned backend 429. The shared frontend
container IP's failed-login counter had reached 5; its block affected clients
sharing that proxy. Unblocked counters also lacked a failure expiry window.

Release `20261018-admin-login` is deployed on the VPS with migration 0017.
Application services were recreated and the existing train/snapshot consumers
updated to the same backend image. The existing backup/deployment procedure
verified database backup `backups/quantik-20261017T172715Z.dump` before migration. No password or role was changed.

- Backend source: `83ade6e`.
- Frontend application source: `a4ed9ed`; browser selector correction `a8fc0e6`.
- Backend image: `sha256:369dfa56acacc0b60d8463fd439f54338363e1accd0332f94442c8f242fc42c4`.
- Frontend image: `sha256:45974567ae32c04a1f1e9f53e0d267f70fb6783b68f0659c5d6aeebcec73e5d6`.

The edge's X-Real-IP overwrite was inspected on production. The API trusts only
the private `web` peer resolved through Docker DNS, and the frontend forwards
the edge header. Nginx client-IP limiting and backend account/IP protection stay
enabled. Failure counters now expire after 15 minutes; blocked responses include
Retry-After and the form displays a countdown while retaining the typed password.

Verification used the public production HTTPS UI and the existing admin
credentials, held in memory without printing them. Browser requests deliberately
included a TEST-NET X-Real-IP header to check edge overwriting. Results:

- Anonymous `/admin` showed the login form without the users menu.
- Login 200, request ID `0392e83d-786d-495a-a0b9-d8bc76e82f2e`.
- Refresh 200, UI logout 204, old access token 401, reloaded anonymous admin gated.
- Database: schema 0017, real admin failures 0 and blocked_until 0; no counter was
  created for the spoofed address or recreated frontend peer.
- Public readiness returned schema 0017.

Focused backend tests passed for token lifecycle, five-attempt protection,
Retry-After, expired windows and trusted/untrusted peer handling. Eighteen Node
checks and four desktop/mobile login browser cases passed. Local production
build and production image builds passed. Credentials and tokens were not saved
in this report or source.
