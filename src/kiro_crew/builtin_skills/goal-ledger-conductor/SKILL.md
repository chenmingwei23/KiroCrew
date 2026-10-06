---
name: goal-ledger-conductor
description: Deprecated alias of goal-conductor, removed next release. The work-ledger conducting procedure now lives in goal-conductor; read that skill instead.
---

# Goal Ledger Conductor (deprecated)

This skill's procedure is now `goal-conductor`, and this name stays for one
release only. `kirocrew-conductor` IS the ledger conductor: it mounts
`@kirocrew-work`, dispatches with bind-before-seed, and settles every `done`
claim with the `accept_eval` tool — so the split this skill existed to hold
open is closed. Read `goal-conductor` and follow it; nothing new should name
this skill.
