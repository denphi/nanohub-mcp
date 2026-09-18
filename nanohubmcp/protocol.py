"""Constants defined by the base MCP protocol itself.

These belong to no single feature: they are names the specification fixes,
used by more than one of this package's modules. Keeping them here is what
lets `tasks` and `server` agree on a wire key without importing each other —
`server` imports the feature modules, so anything they share has to sit
below all of them.
"""

from __future__ import print_function

# The `_meta` key carrying the subscription a notification belongs to. A
# server MUST stamp it on every notification sent for a `subscriptions/listen`
# request, so the client can tell which of its subscriptions was the reason.
MCP_SUBSCRIPTION_ID_KEY = "io.modelcontextprotocol/subscriptionId"
