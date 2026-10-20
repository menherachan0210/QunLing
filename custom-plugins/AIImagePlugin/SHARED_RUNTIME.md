# Shared runtime behavior of the local extension

This source remains based on the official stateless shared-runtime candidate. It is a local deployment customization, not a marketplace certification.

## Per invocation

- Credentials, endpoint, model, size, quality and timeout come from the active installation configuration.
- Each generation/edit constructs and closes its own OpenAI client. No credential or client is cached on the plugin or a Tool component.
- The OpenAI client has `max_retries=0`. A total 300-second deadline covers reference-image preparation and the billable API call.
- Tool response text excludes image bytes, signed URLs and upstream error bodies.

## Scoped temporary image context

- The PersonMessageReceived/GroupMessageReceived listener obtains images and sender identity from explicit serialized Host event fields; bot UUID is resolved through the current query API. The Host-only `event.query` is never read.
- An opaque query reference points to a cache isolated by installation/workspace/bot/conversation/sender. User-supplied Tool parameters cannot select identity or image URLs.
- Each scope retains only its newest source/output image; maximum 32 entries, 64 MiB total and a 30-minute TTL. Successful replies update this cache. Expiration/restart requires a new source image.
- Query variables hold only opaque references and operation status, never image pixels.
- `on_installation_revoked(binding)` removes all entries owned by that installation.
- A process-local lock serializes only the short Host check/claim sequence. Generation happens outside the lock, and generate/edit share the same per-query marker to prevent duplicate billing.

## Validation

Tests use mock provider/Host APIs, including separate installation bindings, message scopes, cache bounds, revocation, repeated calls, timeout and delivery failures. They neither invoke a real image model nor send chat messages.
