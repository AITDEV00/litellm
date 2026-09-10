# 2026-09-08 incident: executive summary

Plain-language version for non-technical readers. The technical detail and
evidence live in the
[incident report](2026-09-08-postgres-disk-full-and-ingress-tls.md) and the
[resolutions document](2026-09-08-resolutions.md)

Status: resolved the same day, with every fix verified working the next morning

## What happened

Our AI gateway keeps a record of every request it handles, stored in a
database, to track usage, watch how the models are performing and confirm
they behave as expected. Those records were configured to grow forever:
nothing was ever deleted. After about two and a half months they filled all
the storage the database had, roughly 50 GB of request records, and the
database stopped working

The gateway felt this immediately. It holds each finished request's usage
record in memory briefly before saving it to the database. With the database
failing, those saves kept failing and the gateway kept retrying them over and
over. That constant churn wore the gateway down until its memory climbed past
its allowance and it crashed. And each time it restarted, it struggled to come
back up, because one of its first steps is connecting to the very database
that was down

The OICM platform then tried to heal itself by giving the database more
storage, but a safety rule meant to prevent overbooking of disk space on the
storage cluster blocked the enlargement. We cleared the block manually,
doubled the storage, and a standby copy of the database took over as the
primary. User requests kept flowing throughout, because the gateway can
answer most of them even when the database is down

## What we changed

Record-keeping now cleans up after itself. Records older than 60 days are
deleted automatically, several times a day, and the deletions are verified to
actually happen. The database stopped growing and now cycles inside a stable
window with plenty of room to spare

The database also got double the storage, so the records cycle inside a
stable window with plenty of room to spare

The gateway no longer depends on the database to serve users or to start up.
With the database completely unreachable it still boots, still answers
requests normally, and reconnects on its own once the database recovers, so
the crash-and-fail-to-boot pattern cannot repeat. If a database failure ever
happens again, the decoupling buys ample time to repair or rebuild the
database calmly, no matter which recovery measures succeed or fail

Each gateway replica was also given more memory, and the gateway keeps a
running diary of its own memory use, collected in our Loki log store, so a
slow creep can be spotted and fixed long before anything crashes. Storage and
memory are now checked as part of routine review, so the next silent fill-up
gets noticed instead of surfacing as an outage

## Where things stand

Service is healthy. The gateway has run without a single restart since the
fixes landed, and the database cycles inside its stable window with room to
spare. The remaining follow-ups are optional improvements listed in the
resolutions document
