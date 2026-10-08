# Operational integration: resuming after a quota outage

`hoh resume-quota` is the code path; this directory holds the wiring that
calls it regularly. **HoH deliberately does not sleep by itself** -- a
controller that holds the lock while sleeping for hours is indistinguishable
from one that has hung.

## What the timer does, and what it does not

| | |
|---|---|
| **does** | checks every run; resumes those carrying `blocked_kind = usage_limit` whose `retry_after` has expired |
| **does not** | lift any other block -- that has a cause a human clears |
| **does not** | start a role run. That needs Herdr; without a session there would be no demonstrable endpoint. |

After a resume the run sits at `ACTIVE` and waits for `hoh run`. That is
deliberate: the timer restores *resumability*, not operation. Anyone who wants
the run continued automatically as well needs a Herdr session for it, and
should do that through a plugin action rather than a timer without a terminal.

The bound against endless loops: after `MAX_RESUME_ATTEMPTS = 5` unsuccessful
attempts the run stays blocked. An automatic retry without a bound costs money
without making progress.

## Two operating modes, two units

| unit | mode | what it runs |
|---|---|---|
| `hoh-resume-quota.{service,timer}` | supervised | `hoh resume-quota`: a due `usage_limit` wait becomes resumable again; nothing is driven |
| `hoh-continue.{service,timer}` | unattended | `hoh resume-quota --drive --recover`: the same, and a resumed project is **driven on** by a new governed master; a project whose master **died** (killed, rebooted away, lease expired) gets a new master (RECOVERY) |

Both resume only `usage_limit` waits. A question collected for the digest, a
contract park and a capability violation keep their owners, and a live master
is never displaced -- only a lease whose holder is provably gone is recovered.
A project takes part in driving only if it registers a driver
(`<root>/projects/<id>/driver.json`).

Which mode a machine is in is derived, not declared:
`python3 tools/unit_drift.py` reports `unattended` when an open project under
the HoH root registers a driver, and then requires `hoh-continue.*` installed,
identical to `ops/`, its timer active and its service not failed. The check
only reads; installing or enabling a unit is always the operator's action.

The behaviour of `hoh-continue.service` is what `tools/durability_e2e.py`
exercises: its tick is built from this unit's `ExecStart`.

## Setting it up (systemd, per user)

```sh
mkdir -p ~/.config/systemd/user
cp ~/hoh/ops/hoh-resume-quota.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hoh-resume-quota.timer
```

Checking:

```sh
systemctl --user list-timers hoh-resume-quota.timer
systemctl --user status hoh-resume-quota.service
journalctl --user -u hoh-resume-quota.service -n 20
```

Turning it off -- the way back is one line:

```sh
systemctl --user disable --now hoh-resume-quota.timer
```

**Per user, not system-wide.** The service needs the captain's provider login
and writes exclusively into `~/hoh/runs`; as a system unit both would be
wrong. `ProtectHome=read-only` with a single `ReadWritePaths` enforces that
instead of promising it.

> **WSL2:** `systemd --user` runs there only if `systemd=true` is set in
> `/etc/wsl.conf`. On this machine it is. Otherwise the cron variant applies.

## Setting it up (cron)

See `cron.example`. Same effect, no sandboxing.

## As a Herdr action

```sh
herdr plugin action invoke resume-quota --plugin hoh
```

By hand, for when one is sitting in Herdr anyway -- and the evidence that the
same code path is reachable through all three routes.
