# Harness Plugin Adapter

Runs one plugin written for the DeepSeek Harness, unmodified, in its own process,
and contributes that plugin's projected view to a member through the
contribution protocol (`docs/system-specs/modules/contribution-protocol.md`).

Naming the hosted runtime is this app's purpose, so it is named here. Nothing in
this app compares it to anything.

## What is hosted

`@deepseek-ai/dsh-session-stats` — a projection unit that folds a transcript into
whole-log counts and wall times. It was chosen because its contribution is one
projection and nothing else: it declares `inject = ['sessionProjections']`, calls
`ctx.sessionProjections.register(...)` once, and touches no other service, no
model, no agent loop, and no browser code.

The plugin is **not vendored and not built**. It is loaded from a read-only
checkout, in source form, so its diff against upstream is empty by construction.
Two Node built-ins make that possible and are the reason the adapter has one npm
dependency (`zod`, which the plugin itself declares):

- `module.registerHooks` resolves each `@deepseek-ai/…` specifier to the file the
  package's own `exports` map names, rewritten from build output to source.
- `module.stripTypeScriptTypes` compiles the TypeScript in memory, in
  `transform` mode — `strip` is not enough, because the runtime's kernel uses
  `const enum`, `namespace`, and constructor parameter properties.

The runtime that then drives the fold is the plugin's own: its DI kernel, its
projection registry service, its schema validation. The adapter supplies the
event stream and the session object the registry keys state by, and nothing else.

Licensing: the checkout is MIT (root `LICENSE`, "Copyright (c) 2026 DeepSeek");
its vendored kernel is MIT ("Copyright (c) 2021-present Shigma", upstream
`cordiverse/cordis`), and the checkout's `THIRD_PARTY_NOTICES.md` records both.
Loading source in place copies nothing into this repository.

## What the numbers mean

The plugin folds an agent-loop transcript. A member's log records something
else, so three of its event types are mapped onto the shape the fold reads:

| member envelope  | plugin event                        |
| ---------------- | ----------------------------------- |
| `slot/opened`    | `step/start`                        |
| `member/message` | `assistant/chunk` (a text delta)    |
| `slot/closed`    | `assistant/message` then `step/end` |

So for a member: `turns` is distinct slots driven, `steps` is fully observed
slot spans, `llmMs` is total wall time holding a slot open, and `ttftMs` is time
from opening a slot to its first message. `toolMs`, `decodeMs` and
`decodeTokens` stay `0` — a member's log carries no tool pairs and no token
usage, and the adapter does not invent either. The render schema published under
§7 labels every field with both meanings, and leaves the three unfeedable ones
off the card rather than showing a permanent zero.

Two limits, both deliberate:

- A `slot/closed` with no matching open emits nothing, so a span whose start was
  never observed is not counted.
- The fold tracks one open step, because the runtime it comes from has one agent
  loop. A member can hold two slots at once, which that model cannot represent,
  so an overlapping `slot/opened` is skipped and counted rather than replacing
  the live span and losing its wall time.

## How it runs

The declared backend entry point is a Python module that immediately `execve`s
Node, keeping the PID the gateway recorded. That indirection is not a
preference: a builtin app cannot use a file `backend.entryPoint`, because the
spawn resolves the entry against `app_dir(name)` under the Kiro Crew home while a
builtin's files only ever exist in the installed package.

The plugin executes in the adapter's process with the adapter's privileges. The
gateway never runs contributor code — that boundary is this process, not a
sandbox inside it.

## Configuration

`~/.kiro/crew/apps/dsh-adapter/data/config.json`:

```json
{
  "checkout": "/abs/path/to/the/plugin/checkout",
  "gateway": "http://127.0.0.1:5476",
  "token": "<app bearer token>",
  "units": ["kiro"]
}
```

A file rather than the environment, because `minimal_env()` strips anything the
platform does not explicitly pass to a backend. `token` is here because nothing
hands an app backend an outbound credential today; §2 assumes one exists.
`units` is optional — omitted, the adapter asks the dashboard for the member
list, because §3 addresses a unit by id and offers no way to enumerate units.

With any of it missing the adapter still starts and still serves health,
reporting `contributing: false` and the reason. A backend that exited instead
would be restarted forever over a configuration gap.

`GET /health` reports what is mounted and, per unit, how much has been folded,
how many refolds a gap forced, and how many publishes the gateway took.

## Tests

```
cd node && npm install && npm test
```

36 tests, no network beyond loopback. `test/driver.test.mjs` runs against
`fake/gateway.mjs`, which serves §3–§7 over a real loopback HTTP server and
enforces the refusals the real gateway will: an event type or key outside the
declared patterns, a built-in key, an ungranted unit kind, an equal or lower
`seq`.

Two things are worth knowing about the suite. `test/fold.test.mjs` checks the
hosted fold against the same plugin definition run in a plain `init`/`apply`/`view`
loop, which is what catches the adapter changing the plugin's answer. And the §3
event frames arrive through an injected socket rather than a real WebSocket:
Node ships a WebSocket client but no server, and the point of the fake is to be
able to **drop** a frame on purpose — a gap the adapter folds across is a view
that stays wrong with no error anywhere.
