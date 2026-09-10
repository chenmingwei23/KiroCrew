# Contribution protocol (draft contract)

Owners: `kiro_crew.eventlog` (log, projections), the dashboard eventlog routes, `website/src/state/memberProjectionStore.ts`

Status: contract draft. The three workstreams below build against this surface; anything a workstream
needs that is not here is a change to THIS file first.

## 1. Purpose

An out-of-process contributor — an app backend, or an adapter hosting a foreign plugin model — can read a
unit's append-only log, append events into it under its own namespace, and publish projected views that
the gateway treats exactly like its own. The gateway stays the only writer of every log, enforces
ownership and quota at the boundary, and pushes contributed views to dashboards with the same
whole-value frames it uses for built-in projections. Nothing in this protocol depends on the
contributor's language.

The unit kinds are those with a log. Today: `member`. The protocol is written for any kind.

## 2. Identity and authority

A contributor authenticates as an **app** with the app token it already holds. Everything it may do is
declared in its manifest:

```json
"contributions": {
  "events":      ["myapp/*"],
  "projections": ["myapp/*"],
  "units":       ["member"]
}
```

- An event `type` a contributor appends MUST match one of its declared `events` patterns, and every
  pattern MUST begin with the app's own name followed by `/`. The gateway refuses anything else.
- A projection `key` a contributor publishes MUST match one of its declared `projections` patterns,
  same prefix rule. Built-in keys (`roster`, `activity`, `wake`, `driving`) cannot be published from
  outside.
- `units` lists the kinds the contributor may subscribe to and append to. An app that declares none
  can do nothing here.
- Reading is granted per kind, not per event type: a subscriber sees every event of a unit it may
  subscribe to. Sensitive data therefore does not belong in an event; it belongs in the durable store the
  event points at.

Declaring `contributions` grants the HTTP paths and frames below; no separate `permissions.api` entry is
needed. Widening a declaration on upgrade is a new consent, handled like any other widened permission.

## 3. Reading: catch-up then stream

```
GET /api/eventlog/{kind}/{id}/events?after=<seq>&limit=<1..500>
  -> { "kind", "id", "events": [envelope...], "lastSeq": n }      oldest first, seq > after
```

Over the app's WebSocket:

```
-> { "type": "eventlog_subscribe",   "data": { "kind", "id" } }
<- { "type": "eventlog_subscribed",  "data": { "kind", "id", "lastSeq": n } }
<- { "type": "eventlog_event",       "data": { "kind", "id", "event": envelope } }   one per append
-> { "type": "eventlog_unsubscribe", "data": { "kind", "id" } }
```

`eventlog_subscribed` is sent before any `eventlog_event` for that subscription. The event channel is a
**delta channel**: the consumer MUST check `event.seq === last + 1` and, on a gap, drop its fold and
re-read from `GET .../events?after=` — never fold across a gap. A subscriber that disconnects resumes
by catch-up from its last folded seq, then re-subscribes. The gateway may close a slow subscriber; a
closed subscriber resumes the same way.

## 4. Appending

```
POST /api/eventlog/{kind}/{id}/events
  { "type": "myapp/thing-happened", "data": {...} }
  -> 201 envelope            | 403 code=event_type_not_owned | 404 code=unit_not_found
                             | 413 code=event_too_large | 429 code=quota_exceeded
```

The gateway assigns `seq` and `time`; a contributor never supplies them. `data` is limited to 64 KiB
serialized. Each app has a per-unit event budget (default 10,000 events per unit per day); over budget
is refused, never queued.

## 5. Publishing a projection

```
POST /api/eventlog/{kind}/{id}/projections/{key}
  { "value": <json>, "seq": n, "stateVersion": v }
  -> 204                      | 403 code=projection_key_not_owned | 409 code=stale_seq
```

`seq` is the log position the view is current as of. The gateway keeps one row per `(kind, id, key)`:
a publish with `seq` lower than or equal to the stored row is refused with `409 stale_seq` (a replay,
or a slower contributor); higher wins and is pushed to dashboards as the existing
`member_projection` frame `{slug, key, value, seq}` (for kind `member`; other kinds get a frame of the
same shape named for the kind). Contributed rows appear in the unit's `projections.values` block next
to built-in keys, so a dashboard needs no new code path to receive them.

`stateVersion` is the contributor's fold version. A publish with a higher `stateVersion` than the stored
row replaces it regardless of `seq`, so a contributor that changed its fold can re-publish from zero.

Folding happens in the contributor's process, against events read through §3. The gateway never
executes contributor code.

## 6. Teardown

Disabling or uninstalling an app: the gateway closes the app's subscriptions, deletes every projection
row the app published (dashboards receive a frame with `value: null` for each key), and refuses further
appends. Events the app appended stay in the log — they are history, and the log is never rewritten.

## 7. Rendering contributed views

A dashboard surface that shows a unit renders unknown `<app>/<key>` views generically: a card titled
by the key, body rendered from the value by a small declarative schema the contributor may publish once
per key (`POST .../projections/{key}/schema`, fields: `title`, `kind: badge | text | list | table |
keyvalue`, `path` selectors). No contributor code runs in the browser.

## 8. Adapters

An adapter is an app whose backend process hosts a foreign plugin runtime and implements that runtime's
service surface as shims over §3–§5 and over the existing MCP and cron surfaces. The plugin code is
unmodified. Each foreign plugin model gets one adapter; the protocol does not change per adapter.

## 9. Error codes

Every error response carries a machine-readable `code`: `event_type_not_owned`,
`projection_key_not_owned`, `unit_not_found`, `unit_kind_not_granted`, `event_too_large`,
`quota_exceeded`, `stale_seq`, `invalid_after`, `invalid_limit`, `invalid_projection_value`.
