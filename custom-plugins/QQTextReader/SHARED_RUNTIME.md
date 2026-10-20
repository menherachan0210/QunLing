# Shared runtime contract

The plugin has no credentials or per-installation configuration. Cache ownership includes
the trusted installation binding's instance, Workspace, installation UUID, runtime revision
and placement generation. Trusted query scope is checked against that binding. Dedicated
workers use a random process-local installation owner; the SDK dedicates each worker to
one installation/Workspace. Bot, chat type, chat and sender are included in each scope.

Query variables contain only an opaque owner, scope hash, status and cache token. Both the
Tool and prompt listener validate that the referenced cache entry actually still exists.
No filename, file ID, URL, attachment bytes or document text enters a system prompt.
The notice Runner derives installation scope from the binding or trusted Host metadata,
never from fields inside a platform notice. It captures metadata only and performs no I/O.

The process cache has a threading lock, a 30-minute TTL, a 64-entry limit and an 8 MiB
aggregate bound. Duplicated file IDs do not refresh age. Installation revocation removes
all cache entries for that exact binding. Process restart clears dedicated-worker state.
No file data is written to disk by this plugin.
