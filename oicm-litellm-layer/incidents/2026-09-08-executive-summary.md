# 2026-09-08 incident: executive summary

Plain-language version for non-technical readers. The technical detail and
evidence live in the
[incident report](2026-09-08-postgres-disk-full-and-ingress-tls.md) and the
[resolutions document](2026-09-08-resolutions.md)

Status: resolved the same day, with every fix verified working the next morning

## The problem in plain language

Our AI gateway is the front door internal products use to reach AI models.
Behind it sits a database that keeps a record of every request, mainly for
billing. That record was configured to grow forever: nothing was ever deleted.
After about two and a half months the records filled all the storage the
database had, roughly 50 GB of them, and the database stopped working

The platform tried to heal itself by giving the database more room, but a
storage safety rule, meant to prevent overbooking of disk space on the storage
cluster, blocked the enlargement. We enlarged the space manually and the
database recovered, with a standby copy taking over as the primary in the
meantime. Requests kept working throughout, because the gateway can answer
most of them even when the database is down

Two older problems surfaced the same day. The gateway's public web address had
been serving a security certificate that did not match its name since June,
which strict clients outside our network would refuse. And none of our
automatic alarms fired: one alarm reads a measurement our current servers no
longer produce, and the alarm delivery rules send everything to recipients
that were never configured

Separately, the gateway itself had crashed twice in the days before the
database trouble, because its memory allowance was too small for how it
buffers work under load

## The solution in plain language

Record-keeping now cleans up after itself. Records older than 60 days are
deleted automatically, several times a day, and the deletions are verified to
actually happen. The database stopped growing and now cycles inside a stable
window with plenty of room to spare

The database got double the storage, and the storage safety rule was raised so
future enlargements complete on their own instead of needing manual help. The
enlarged setup is also stored in our configuration repository, so a routine
platform upgrade cannot quietly shrink it back

The gateway no longer depends on the database to serve users. With the
database completely unreachable it still starts, still answers requests, and
reconnects on its own once the database returns. Its memory allowance was
raised, and it now keeps a running diary of its own memory use, so a slow
creep can be spotted and fixed long before anything crashes. Profiling with
real traffic confirmed the serving path does not leak

The certificate problem was fixed by pointing both public addresses at the
matching certificate we already owned. Verified from outside the network: the
main address now serves the correct certificate

On alarms we made a deliberate choice rather than a quick patch. Because the
gateway now survives a total database failure, an automated database alarm is
no longer needed for this risk; the memory diary plus a short manual
checklist, kept in the incident report, cover monitoring instead. The broken
alarm delivery itself is a platform-wide issue and stays open with the
platform team

## Where things stand

Service is healthy. The gateway has run without a single restart since the
fixes landed, both public addresses serve correct certificates, and the
remaining follow-ups are optional improvements listed in the resolutions
document
