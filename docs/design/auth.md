# Authentication and authorization design

Built from scratch, with no managed identity service. Code: `lambda/auth/auth_handler.py`,
`lambda/authorizer/authorizer_handler.py` and `lambda/shared/medislot_common/tokens.py`.

## Tokens

| Token | Format | Lifetime (SSM) | Stored |
|---|---|---|---|
| Access | JWT, HS256. Payload has exactly `sub` (userId), `role` (doctor or patient), `iat`, `exp`, `jti` | 15 min (`auth/access-token-seconds`) | Web app server-side session only |
| Refresh | Opaque, 48 random bytes (URL-safe) | 7 days (`auth/refresh-token-seconds`) | Only its SHA-256 hash, in DynamoDB `refresh-tokens` (TTL on expiresAt) |

**Issuer.** The authorizer checks the issuer on every request. The payload must hold exactly the five claims, so the
issuer (`auth/jwt-issuer`, e.g. `medislot-dev`) goes in the signed JWT header as `iss` (RFC 7519 §5.3 allows a claim
to be repeated as a header parameter). The header is part of the signing input, so it cannot be altered.

**Signing secret.** An SSM SecureString (`/medislot/<env>/auth/jwt-secret`, aws/ssm key), seeded once by
`scripts/seed_secrets.sh` and read by the Auth Lambda and the authorizer at cold start. It is never in source,
never in an environment variable, and can be rotated without a code change (every user then logs in again).

## Flows

- **Register** (`POST /auth/{doctors|patients}/register`): password policy (at least 10 characters from the auth stack, one
  letter, one digit, at most 72 bytes because bcrypt ignores anything longer), bcrypt with the auth stack's work factor
  (12; the Lambda refuses anything lower), then one transaction inserts the users row and the doctors or patients
  row. Email is unique across both roles (409). A doctor registers with a licence number (unique, 409).
- **Login** (`POST /auth/{role}s/login`): looks the user up by email and portal role and checks bcrypt. A wrong
  password, an unknown email, a deactivated account and the wrong portal all get the same 401. An unknown email is
  checked against a dummy cost-12 hash so its timing matches a real account. Emits `LoginSuccesses` and
  `LoginFailures` metrics.
- **Refresh** (`POST /auth/refresh`): looks up the hash and checks the user is still active, then one DynamoDB
  transaction deletes the old hash (conditional on owner and expiry) and puts the new one. A refresh token works
  once; replaying it gets 401.
- **Logout** (`POST /auth/logout`, protected): deletes the refresh token (it must belong to the caller) and writes
  the access token's `jti` to `token-revocations` with `expiresAt` = the token's own `exp`, so the denylist entry
  lives exactly as long as the token would have. The next request with that token gets 401.

## Lambda authorizer (TOKEN type, cache TTL 0)

1. Bearer token from the Authorization header (API Gateway first checks the format with a regular expression).
2. Signature with HS256 pinned (`alg: none` or any other algorithm is refused), then issuer, then the exact claim
   set, then expiry.
3. `jti` against the denylist (consistent read). If the denylist cannot be read, the request is refused (fail closed).
4. The role must be allowed on the route, which is taken from the methodArn:

| Route | Roles |
|---|---|
| `/auth/logout` | doctor, patient |
| `/doctors/me`, `/doctors/me/*` | doctor |
| `/patients/me`, `/patients/me/*` | patient |
| `/doctors`, `/doctors/{doctorId}`, `/doctors/{doctorId}/slots` | patient |
| `/appointments*`, `/slots/{slotId}/waitlist`, `/waitlist-offers/{offerId}/accept` | patient |

5. Returns an IAM policy for the exact methodArn and the context `{userId, role, jti, exp}`.

| Case | Result |
|---|---|
| No token, malformed, tampered, wrongly signed, wrong issuer, expired, logged out | 401 (`raise Exception("Unauthorized")`) |
| Valid token, wrong role for the route | 403 (Deny policy) |

**Why the cache TTL is 0.** With any authorizer caching, a token used after logout would still be accepted until the
cache entry expired, and a cached Allow for one route could not be reused safely for another route. Every request
is checked; the authorizer is small (256 MB, one consistent DynamoDB read) and the cost is acceptable at this scale.

## Handlers read the caller only from the authorizer context

`medislot_common.http.identity()` reads `requestContext.authorizer.userId` and `role` and nothing else. No route takes
a user, doctor or patient id for "me" operations, and request bodies are validated with `additionalProperties:
false` (API Gateway models and the Lambdas), so `{"patient_id": "<someone else>"}` is a 400. A second role check in
each Lambda is a backstop behind the authorizer.
