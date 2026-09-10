# Security Policy

## Scope

This repository contains `hoh`, a research-preview command-line workflow tool. In scope for security reports:

- The `hoh` Python package under `src/hoh/**`.
- The way it invokes the external `herdr` binary as a subprocess and persists run state under `runs/`.

Out of scope:

- The `herdr` binary itself is not part of this repository; report issues in it to its own maintainers, wherever that project is hosted.
- Deployment, hosting, or infrastructure choices made by whoever runs `hoh` -- this project ships as a local CLI, not a hosted service, and takes no responsibility for how it is deployed.

## Reporting a vulnerability

This is a one-person research preview with no dedicated security contact, issue tracker, or mailing list configured at this time. That gap is stated here rather than papered over with a contact method that does not actually exist. If you have found a security issue, use whatever channel you already have to reach the maintainer of the source you obtained this code from (for example, wherever you cloned or downloaded it), and describe enough to reproduce it: the affected file(s), the input or command that triggers the issue, and the observed versus expected behavior.

Please do not open a public report for a vulnerability that is not yet fixed; use a private channel to the maintainer as above until a dedicated one exists.

## What to expect

No response-time commitment is made here, because none has been agreed to. Promising a turnaround without a process behind it would be exactly the kind of unevidenced claim this document exists to avoid. Reports will be read; there is no support contract or service-level agreement backing that.

## Supported versions

There is no versioned release process yet, and therefore no supported-versions table; the most recent state of the default branch is what gets fixed.
