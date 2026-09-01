# Running it for real

The quickstart runs the engine in a terminal. This page is about the next
step — leaving it running — and the ways that has actually gone wrong.

The short version: **`drumbeat service install` generates the right unit for
your platform, starts it, and verifies the engine is actually answering before
it reports success.** Nothing is hand-written. The rest of this page is what
that unit does on your behalf and the failure modes it exists to prevent —
worth reading once, because the service supervises a scheduler whose stop
procedure is not the usual one.

Any process supervisor works — the generated unit is not magic, and the last
section shows the exact invocation to hand to something else. But on Linux and
macOS you should not need to.

---

## 1. One `serve` per workspace. Really.

Two schedulers double-fire every automation. The engine takes an advisory lock
to prevent that — but **the lock is on the data dir, not the workspace.**

Measured, on this codebase: two `drumbeat serve` processes pointed at ONE
workspace with two different `--data-dir` values each acquired their own lock,
each logged `scheduler lock acquired`, and each registered the same automations
on its own cadence. Two engines, one policy tree, both certain they were alone.

```
# BOTH of these start successfully. Do not do this.
drumbeat serve --workspace ~/myspace --data-dir ~/state-a --port 9100
drumbeat serve --workspace ~/myspace --data-dir ~/state-b --port 9101
```

The rule: **one workspace, one data dir, one engine, one port.** If you run
several engines on a host (several consumers, or a test instance beside a real
one), give each its own workspace *and* its own data dir, and write down which
port belongs to which — nothing on the machine will tell you later.

The startup line names the data dir it locked. Read it.

### Split the data dir out once the workspace is under git

The quickstart puts everything under one directory, because one directory is
simpler to start with. The moment your workspace becomes a git checkout —
which is the right thing to do with policy you care about — separate them:

```bash
drumbeat serve --workspace ~/myspace --data-dir ~/myspace-state --port 9100
```

Run artifacts, the outbox, the locks, the API key and the session pins are
*engine state*: written only by the engine, never hand-edited, never worth
versioning. Automations, guidance, prompts and `drumpacks.txt` are *policy*: yours
to author and commit. With one directory, `git clean -fdx` in your policy tree
destroys server state. With two, it is structurally incapable of it.

**Pass `--data-dir` to every command or none of them.** `doctor`, `drain`,
`sweep`, `sessions` and `api-key` all read that directory; pass it
inconsistently and they will report on an empty one while your real engine runs
elsewhere. `drumbeat doctor` prints a containment warning until you split them.

---

## 2. Stopping it: drain, then kill by explicit pid

**Never `pkill -f drumbeat`.** In this project a pattern-matching kill has
matched its own invoking shell and killed a live service four separate times.
The engine's own in-flight check builds its process list in Python for exactly
this reason and matches the marker there — never through a shell `pkill`/`grep`.
On Linux it reads `/proc` directly (so it needs no external binary, and works in
a minimal container that ships no `ps`); elsewhere — macOS has no `/proc` at all
— it parses `ps -eo pid,ppid,args`.

There is a second, worse reason. The runner spawns each turn's worker in its
**own process group** (so the turn's whole tool tree dies together when its
watchdog fires) with `close_fds=True`. Kill the scheduler mid-turn and that
worker **keeps running and keeps writing the session transcript**, while the
kernel releases the parent's per-session lock the instant the parent dies. The
next scheduler then resumes that same session underneath the still-writing
orphan — the exact transcript corruption the locks exist to prevent, performed
deliberately.

So the stop procedure is three steps, in order:

```bash
# 1. Stop starting new runs, and block until none are in flight.
#    --reason is required. A drained engine that cannot say why it is drained
#    is indistinguishable from a broken one.
drumbeat drain --workspace ~/myspace --reason "picking up engine edits" --wait

# 2. Kill the scheduler BY EXPLICIT PID. Get it from the drain output or from
#    `drumbeat doctor`; confirm it with `cat /proc/<pid>/cmdline` (Linux) or
#    `ps -p <pid> -o args=` (macOS).
kill <pid>

# 3. Start again, and CLEAR THE DRAIN.
drumbeat serve --workspace ~/myspace --port 9100 &
drumbeat drain --workspace ~/myspace --clear
```

**Step 3's clear is not optional, and forgetting it is silent.** The drain flag
is a *file* in the data dir, so it outlives the process that set it. Start the
engine with the flag still present and it comes up holding the lock, answering
`/api/health` with `ok`, reporting `doctor` FRESH — and scheduling nothing,
forever, with every surface saying healthy.

Two things make that survivable rather than fatal: `serve` prints an
unmissable `DRAINING -- NO SCHEDULED RUN WILL START` banner as the last thing
before its loop, and `drumbeat doctor` reports `draining: YES` with the reason.

**The installed service does all three steps for you.** The generated unit's
`ExecStop` is the blocking drain, its stop signals the main process only
(`KillMode=process`, so in-flight children survive), and its `ExecStartPre`
clears the drain on the *start* path — so a crash-restart can never strand the
engine drained. The manual sequence above is for a foreground `serve`; once
`drumbeat service install` is in place, a `systemctl --user restart drumbeat`
is the whole procedure.

---

## 3. Linux + systemd (the reference deployment)

Install it as a `systemd --user` service:

```bash
drumbeat service install --workspace ~/myspace --data-dir ~/myspace-state --port 9100
```

That one command generates `~/.config/systemd/user/drumbeat.service`, runs
`daemon-reload`, `enable`, and `restart`, then **probes `/api/health` and
refuses to report success until the engine actually answers holding its
scheduler lock.** An install that started a unit which never bound its port is
exactly the "up but useless" state the health probe exists to catch; you get a
named failure with a `journalctl` pointer, not a false green.

**Then it runs one real turn and checks the answer.** Health proves the HTTP
face is up; it says nothing about whether a scheduled run would work. So
install submits one turn through the running unit against a throwaway
automation, and the turn is asked for a **sentinel**: the single word `READY`.

The gate passes only if the reply *is* that sentinel — markdown decoration
around it (`**READY**`) is tolerated, prose containing it is not. Anything
else fails, naming expected-vs-got. This is deliberate and it is the point:
a gate that accepted any non-empty reply would certify the reply
`Error: No providers available` as a verified turn — an engine with no brain,
reporting three green signals. A false FAIL costs you one re-run; a false PASS
is the whole failure class (`docs/VISION.md` §4). Pass `--skip-turn-verify` to
install without the check; it prints an unmissable notice that it did not look.

```bash
drumbeat service status      # unit state + a live health probe
drumbeat service uninstall   # stop (drains first), disable, remove, verify gone
```

You do not hand-write the unit — but the parts of it that are not obvious are
still worth knowing, because they are the reason the stop procedure below is
what it is:

- **`ExecStop` is the drain**, blocking, with a `TimeoutStopSec` longer than the
  drain's own timeout, so systemd never `SIGKILL`s a drain that is legitimately
  waiting on a real turn. `drumbeat service uninstall` (and a plain `systemctl
  --user stop drumbeat`) therefore drains before it kills.
- **`KillMode=process`.** Signal only the main process, never the control group:
  in-flight agent children are exactly what must not be killed with the
  scheduler.
- **`ExecStartPre=` clears the drain** that `ExecStop` set, on the *start* path
  and with a `-` prefix, so a crash-restart cannot strand the engine
  drained-but-healthy-looking and a clear that finds no drain is a no-op.
- **The provider key goes in the unit's environment**, via an optional
  `EnvironmentFile=-%h/.config/drumbeat/drumbeat.env`. A login shell's `export`
  is invisible to a service; put `ANTHROPIC_API_KEY` (and any pack config)
  there. macOS reads that same file through a generated exec wrapper — see
  section 5.
- **`AMPLIFIER_AGENT_WORKSPACE` is never set.** It silently re-buckets every
  session id under a slug no other launch path derives; the unit pins
  `WorkingDirectory` to the workspace instead. `drumbeat doctor` reports when
  the variable leaks in from an operator's shell.

**Linger.** `systemd --user` services stop when your last session ends. To keep
the engine running across logout and reboot, enable lingering once — `install`
prints this reminder, and reports whether it is already on:

```bash
loginctl enable-linger "$USER"
```

---

## 4. WSL

**Keep the workspace and the data dir on the Linux filesystem (`~/...`), never
under `/mnt/c`.**

The single-scheduler guarantee and the per-session transcript guard are both
POSIX advisory locks (`flock`). On `drvfs` — the `/mnt/c` mount — advisory
locking is not reliably enforced, so `flock` can *appear* to succeed for two
processes at once. Every mechanism in this engine that prevents double-firing
and transcript corruption is built on that call. A data dir on `/mnt/c` does
not merely run slower; it removes the guarantee while continuing to print
`scheduler lock acquired`.

*Not measured on this machine* — stated from the mechanism, not from a WSL
reproduction. Treat it as a hard constraint anyway: the failure mode is silent
and the cost of complying is a different directory.

The engine has widened its lock error handling so that a filesystem which
refuses or cannot support the lock produces a named refusal to start rather
than a raw traceback. It still cannot detect a filesystem that *pretends* to
lock.

---

## 5. macOS

- **There is no systemd; `launchd` is the native answer.** The same command
  installs it — `drumbeat service install` writes a LaunchAgent to
  `~/Library/LaunchAgents/drumbeat.plist`, loads it, and verifies `/api/health`
  and the one real turn the same way it does on Linux. `status` and `uninstall`
  work the same too. `RunAtLoad` starts it now and at login; `KeepAlive`
  restarts it on a non-clean exit (launchd's `Restart=on-failure`). launchd has
  no `ExecStop` hook, so the graceful drain-on-stop is Linux-only — on macOS a
  stop is a plain `SIGTERM`, so prefer stopping the engine when nothing is
  mid-turn.

- **The provider key reaches the unit through a generated exec wrapper.**
  launchd has no `EnvironmentFile=`. Its only env hook is the plist's
  `EnvironmentVariables` dict — literal values, in a world-readable file that
  `service install` rewrites on every run. So the key is *not* put there.
  Instead `install` also writes
  `~/.config/drumbeat/drumbeat-launchd-exec.sh`, makes it
  `ProgramArguments[0]`, and that wrapper sources
  **`~/.config/drumbeat/drumbeat.env` — the same file the systemd unit
  references** — before `exec`ing the real invocation. One env file, one
  semantics, both platforms; the secret never enters the plist.

  Put `ANTHROPIC_API_KEY` (and any pack config) in that env file as plain
  `KEY=value` lines. A missing file is a no-op, exactly like systemd's leading
  `-`. The wrapper is regenerated by every `install`, so a plist regeneration
  can never silently drop it — and `uninstall` removes the wrapper while
  leaving your env file alone.

- **The unit's `PATH` includes Homebrew.** `install` bakes a `PATH` resolved
  from the installing shell. On macOS it also adds `/opt/homebrew/bin` and
  `/opt/homebrew/sbin` — the Apple Silicon prefix, which no service manager's
  default `PATH` contains (the defaults name `/usr/local`, Homebrew's *Intel*
  prefix). Without it a LaunchAgent cannot see any brew-installed tool a turn
  shells out to, while `/api/health` still answers `ok`.

- **`drumbeat drain --status/--wait` works here.** macOS has no `/proc`, so
  process inspection falls back to `ps -eo pid,ppid,args` — never a shell
  `pkill -f`/`grep`. If inspection is impossible at all (no `ps`), the drain
  check reports `NOT DRAINED` naming that as the blocker rather than returning
  an empty turn list that would read as "all clear".

- **A closed lid is not unattended.** A sleeping Mac does not run your
  scheduler. Interval schedules (`every 30 minutes`) resume from the moment the
  machine wakes; a `daily at HH:MM` occurrence that passed while the machine
  was asleep is **skipped, not run late** — deliberately, so an outage costs at
  most one missed day rather than a burst of catch-up runs. If "it fires while
  I sleep" is the reason you want this, the engine needs to be on a machine
  that is awake.

---

## 5a. Any other supervisor

`drumbeat service install` covers Linux and macOS. Anything else — a container
entrypoint, a BSD `rc` script, a supervisor you already run — supervises the
engine perfectly well; it is an ordinary long-running process. Point it at the
absolute invocation and let it restart on failure:

```bash
drumbeat serve --workspace ~/myspace --data-dir ~/myspace-state --port 9100
```

The two things such a wrapper must get right are the two the generated units
handle for you: send the stop signal to the **main process only** (so in-flight
agent children are not killed with the scheduler — see section 2), and put the
provider key in the process environment rather than a login shell.

---

## 6. The m365 pack is the advanced track

The example automations that touch mail, chat, calendar and meetings need a
pack that talks to Microsoft Graph. That is a genuinely harder setup than
everything on this page: an app registration, delegated scopes, admin consent
in your tenant, and a token store — plus Conditional Access policies that can
refuse a non-interactive refresh for reasons that have nothing to do with this
engine.

**Do not start there.** Start with `session-growth-check.md`, which needs no
pack at all and watches the engine's own session fleet. Add a pack once one
automation has run unattended for a few days and you trust the loop.

When you do get there, the field notes for Graph — the probe-first
methodology, the permission and consent sequencing, the errors that look like
bugs and are policy — are in the `msgraph-integration-patterns` skill rather
than in this repo. Budget an afternoon, not a coffee break.
