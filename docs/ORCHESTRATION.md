# Orchestration and corpus boundary

Minority Report turns reviewed inputs into local candidate research artifacts.
Alexandria is the separate reviewed system of record.

## Flow

1. Resolve pasted material, files, or supported GitHub URLs.
2. Build a draft and price the configured provider calls.
3. Present the exact inputs, models, estimate, ceiling, and confirmation phrase.
4. Dispatch only after explicit confirmation.
5. Preserve raw responses, failures, costs, claims, scores, report, and manifest
   in an immutable local run directory.
6. Let an operator review, redact where authorized, and deliberately promote
   suitable artifacts into an Alexandria branch and pull request.

The application does not write into Alexandria's `research/` tree. A healthy
service or green unit suite does not prove the live provider-to-publication path.

## Ownership

Minority Report defines executable behavior and local record formats needed to
operate the service. Alexandria defines the meaning and governance of promoted
corpus artifacts. When a corpus contract changes, update Alexandria first or in a
coordinated pair, then update and test the reader/writer here. Never publish the
same manual as authoritative in both repositories.

## Provider and cost boundary

Provider calls use locally resolved credentials. Raw responses are captured
before tolerant decoding; failures remain visible. Estimates are not caps, and
agreement is not verification. Provider-backed validation is deliberately
separate from the repository's offline full test path.

## Local run layout

Completed commission directories contain the run metadata, brief, original and
extracted inputs, raw provider/grading responses, claims, scores, report, and a
manifest. They may contain private or licensed material and are not safe to
commit wholesale. Follow Alexandria's public data-handling rules during promotion.
