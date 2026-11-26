# Transport

## Scope

How a request crosses the network boundary between the gateway and a source host. This slice owns protocol mechanics, not deployment status.

## Contents

| File | Kind |
|---|---|
| [`pilot/nginx-reverse-tunnel.md`](pilot/nginx-reverse-tunnel.md) | Historical lab pilot, the flow that preceded the broker |

## Where current transport behavior lives

The pilot here is a record of the lab setup that proved the approach. Current transport
behavior is documented in [`../running-system/README.md`](../running-system/README.md),
which is the owning record: one `grpctunnel` stream on 8093, with the reverse-h2 long-poll
plane deleted on 2026-11-18. Read that first for what runs today, then this folder for how
the mechanism was proven.
