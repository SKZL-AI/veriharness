# The Herdr plugin, and what was actually measured about it

`plugin/herdr-plugin.toml` is how HoH becomes reachable from inside Herdr
itself, as a set of menu actions rather than a separate command line to
remember. This document describes that plugin, and -- because a plugin
manifest is easy to write against an assumed interface that turns out not to
match reality -- the three properties of Herdr's plugin interface that were
**measured against a running Herdr 0.8.0**, not read out of Herdr's own
documentation and taken on faith.

## What the plugin is for

The plugin's own header comment states its purpose in one word:
**reachability**. Every action it declares is a thin wrapper that shells out
to `bin/hoh`, which in turn calls the same `hoh` CLI documented in
`docs/OPERATIONS.md`. There is no second implementation living inside the
plugin -- if the CLI's behavior changes, the plugin's behavior changes with
it, because there is only one behavior.

## The four actions, and why the list is short on purpose

```toml
[[actions]]
id = "list"       # every known run, with stage, condition, and accepted candidate

[[actions]]
id = "status"     # stage, condition, budget and open items of the most recently touched run

[[actions]]
id = "report"     # a Markdown report: verdicts, receipts, evidence, open gaps

[[actions]]
id = "resume-quota"   # resumes runs blocked on an exhausted provider quota
```

Everything that needs a specific run identifier or an explicit approval --
`run`, `start`, `deliver`, `cancel` -- is deliberately left out of the
plugin. The manifest's own comment explains why: those steps belong at the
CLI with explicit arguments a human typed, not behind a menu item that could
be clicked without the same deliberateness. This is the same principle
`README.md` states about HoH as a whole -- action that spends role-run
budget or commits to a delivery decision needs a human naming exactly what
they mean, not a one-click affordance.

## Three properties measured against Herdr 0.8.0

Each of these was found empirically, against a real running Herdr instance,
not assumed from Herdr's documentation -- and each one changed how
`plugin/herdr-plugin.toml` and `plugin/bin/hoh` are actually written.

**1. The manifest's `[env]` section is not read.** A variable declared there
does not arrive inside the invoked action -- confirmed by observing a
`ModuleNotFoundError` for exactly the variable that `[env]` was supposed to
supply. Any environment an action needs has to come from somewhere the
action itself controls, not from the plugin manifest.

**2. `herdr plugin action invoke` passes through no arguments at all.**
Every action in the manifest is therefore argument-free by necessity -- an
action declared with a required argument would describe an interface that
does not exist. This is the direct reason `run`, `start`, `deliver`, and
`cancel` cannot simply become plugin actions with a `run_id` parameter: the
invocation path has no way to supply one.

**3. Herdr does provide `HERDR_PLUGIN_ROOT`, and sets the working directory
to the plugin's own root.** `bin/hoh` relies on exactly this rather than
assuming a fixed installation path. The first version of this plugin instead
hard-coded a `PYTHONPATH` pointing at one specific machine's checkout
location; measured property 3 is what let that be replaced with something
that works from whatever directory Herdr actually places the plugin's
working directory in, on any machine that installs it.

## What this means for anyone extending the plugin

If you are adding an action: it will receive no arguments and no
manifest-declared environment variables, no matter what the manifest looks
like. Build the action's script to derive everything it needs from
`HERDR_PLUGIN_ROOT` and the current working directory Herdr sets, the same
way `bin/hoh` already does -- and if you need to re-verify any of the three
properties above against a newer Herdr release, that is a measurement to
repeat against the real running plugin host, not an assumption to carry
forward from this document.
