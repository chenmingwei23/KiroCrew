# Endpoint-security software blocking agent-spawned git/cr

**Symptom.** When the agent runs a command on your behalf — `git` (even a
read-only `git branch`), `cr`, or another tool — your endpoint-security product
(an "EDR", for example an injected behavioral sensor) kills it with a message
like:

> A process was blocked because malicious behavior was detected.

The same `git` and `cr` commands work when **you** type them in your own
terminal. Only the processes launched *by the agent* are blocked, so anything
that needs to spawn git — status, fetch, branch, commit, code review — fails.

This is cross-platform: it happens on any OS where a behavioral sensor is active
(Windows, macOS, Linux). It is distinct from the desktop-app renderer case
covered in the [Windows guide](windows-install.md) (where an injected DLL blocks
the browser's child processes); here it is the ordinary command-line tools the
agent spawns that are blocked.

## Why it happens

For safety reasons Kiro Crew does not launch tools directly. It starts each one
through a tiny exec-shim that applies the child's process setup (resource limits,
working directory, controlling terminal) and then replaces itself with the real
command. The reason is a deadlock- and fd-leak class that the obvious
"configure the child, then run it" approach reintroduces on the gateway's event
loop; running the setup in a process that then execs the command avoids it.

To a behavioral EDR, "an interpreter starts and immediately replaces itself with
another binary" matches a generic living-off-the-land / process-replacement
heuristic, so the whole process tree the agent spawns gets flagged — regardless
of the actual command. **It is a false positive:** the behavior is benign and
the spawned command is an ordinary `git`/`cr` invocation. No change to how the
tool is spawned reliably stops a behavioral sensor from flagging it, because the
sensor is reacting to the generic shape, not to anything specific; and the setup
the shim performs is a correctness requirement, not a toggle.

## The durable fix: a process exclusion

Add a **process exclusion** in your endpoint-security product for the Kiro Crew
executable **and the processes it launches**, so the sensor stops killing the
agent's child processes. This is the same remedy the
[Windows guide](windows-install.md) recommends for the renderer case: it
addresses the cause and leaves everything else protected.

In most products a policy exception has to name both:

- the executable (the Kiro Crew application / CLI), and
- the detection that fired — the detection id or the exact block text from the
  security console.

Open your security product's console (or the OS event log) and read the
detection entry to get the id and the module/command it blocked, then hand those
to whoever administers endpoint security on the device. That ownership usually
sits with an endpoint-security or IT team rather than with the Kiro Crew user,
because adding the exception requires policy access the user does not have.

## Interim workaround

If you cannot get the exclusion added right away, run `git` and `cr` **from your
own terminal, outside the agent.** Those commands are not blocked when you run
them yourself, and doing so changes nothing about your Kiro Crew install, so it
is safe to leave in place for as long as you need. You lose the convenience of
the agent running them for you, but you are not blocked on migrating or working.

## What this is *not*

- **Not an SSH or credential problem.** A missing or removed SSH agent socket
  shows up as a `publickey`/permission-denied failure on `fetch` and `push`.
  `git branch` needs no SSH at all, so a sensor killing `git branch` is this
  behavioral-block issue, not an SSH one — do not wait on an SSH fix for it.
- **Not something a Kiro Crew setting can turn off.** There is deliberately no
  switch to make the agent spawn tools "unwrapped": the shim exists to prevent a
  gateway deadlock and a listening-socket fd leak, and removing it to appease a
  false positive would reintroduce both. The exclusion belongs in the security
  product.
