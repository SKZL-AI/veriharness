"""What a role agent may do without asking, decided before the run starts.

P1-09, Approval Policy (V3.3, Execution Safety Plane). O198: HoH passed no
permission settings to a role, so every Claude agent inherited the operator's
interactive defaults and every Bash command waited for a keypress. The first
fresh-clone run blocked ten seconds into its first dispatch, and because a
blocked dialog aborts the iteration (O199) it could not converge under
supervision either.

The policy is decided up front, written down, and recorded with the run:

* **A mode, named.** `auto` (the harness approves routine actions itself and
  stops risky ones), `dontAsk` (anything not on the allow-list is denied, never
  prompted), or `bypassPermissions` ("yolo", only when a policy says so by
  name). The modes that would still stop for a human -- `default`, `manual`,
  `acceptEdits`, `plan` -- are refused: a policy whose point is "the run does
  not wait for a person" cannot choose a mode that makes it wait.
* **A deny-list that no mode switches off.** Deletion, history rewriting,
  privilege escalation, network egress, publishing and `nvidia-smi` are denied
  in every mode. The list is mandatory: a policy missing any entry of
  `MANDATORY_DENY` is refused on load, so it cannot be weakened by editing the
  file. Deletion is replaced, not merely forbidden: the house-rules prompt tells
  every role to version and archive instead.
* **The house rules travel with the agent.** They are appended to the role's
  system prompt, so they bind the session HoH spawned rather than depending on
  whatever the operator's global configuration happens to say.

What this is not: a security boundary against a hostile model. A role that can
run the tests it wrote can run arbitrary code; the boundary that makes an
acceptance trustworthy is the sandbox the *checks* run in and the independent
receipts, not the agent's tool list. This policy decides what a well-behaved
agent may do without a human, and makes that decision auditable.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = "hoh-role-approval/1"

#: Modes that never stop for a human. Everything else is refused.
MODES = frozenset({"auto", "dontAsk", "bypassPermissions"})

#: Modes a policy may not choose, with the reason it is refused.
REFUSED_MODES = {
    "default": "stops at every unapproved action for a human",
    "manual": "stops at every unapproved action for a human",
    "acceptEdits": "accepts file edits but still stops at every command",
    "plan": "cannot act at all",
}

#: Denied in every mode, and a policy that omits any of them is refused. Each
#: is the Claude permission-rule spelling of something the house rules forbid.
MANDATORY_DENY = (
    "Bash(rm:*)", "Bash(rmdir:*)", "Bash(shred:*)", "Bash(dd:*)",
    "Bash(mkfs:*)", "Bash(find * -delete:*)", "Bash(truncate:*)",
    "Bash(git clean:*)", "Bash(git reset --hard:*)", "Bash(git push:*)",
    "Bash(git branch -D:*)", "Bash(git tag -d:*)", "Bash(git rebase:*)",
    "Bash(sudo:*)", "Bash(su:*)", "Bash(chown:*)",
    "Bash(curl:*)", "Bash(wget:*)", "Bash(ssh:*)", "Bash(scp:*)",
    "Bash(twine:*)", "Bash(gh release:*)", "Bash(npm publish:*)",
    "Bash(nvidia-smi:*)",
    "WebFetch", "WebSearch",
)

#: Allow rules too broad to be a decision about anything.
_UNSCOPED = re.compile(r"^(Bash|Write|Edit|NotebookEdit)(\(\s*\*?\s*(:\s*\*)?\s*\))?$")

#: What the house-rules prompt must say, checked on load so it cannot be
#: emptied into a formality.
_REQUIRED_PHRASES = ("never delete", "version", "archiv", "nvidia-smi")


class PolicyRefused(ValueError):
    """The policy would not do what a policy is for; nothing was applied."""


@dataclass(frozen=True)
class RoleApprovalPolicy:
    mode: str
    deny: tuple[str, ...]
    house_rules: str
    allow: dict[str, tuple[str, ...]] = field(default_factory=dict)
    source: str = ""
    digest: str = ""

    def write_role_files(self, run_dir: Path, role: str) -> tuple[Path, Path]:
        """The settings file and the house-rules prompt for one role, written
        into the run directory.

        Files rather than arguments because Herdr starts an agent by typing
        its command into a shell: `Bash(rm:*)` carries parentheses and a glob,
        and the house rules are several lines long. A path survives any
        quoting; a rule list might not, and a deny rule that the shell ate
        would be a deny rule that silently does not exist. The files stay in
        the run directory as the evidence of what each role could do.
        """
        folder = Path(run_dir) / "approval"
        folder.mkdir(parents=True, exist_ok=True)
        settings = folder / f"{role}.settings.json"
        prompt = folder / "house_rules.md"
        settings.write_text(json.dumps({"permissions": {
            "defaultMode": self.mode,
            "deny": list(self.deny),
            "allow": list(self.allow.get(role) or ()),
        }}, indent=2) + "\n", encoding="utf-8")
        prompt.write_text(self.house_rules + "\n", encoding="utf-8")
        return settings, prompt

    def args(self, role: str, settings: Path, prompt: Path) -> list[str]:
        """The arguments that put one Claude role session under this policy:
        plain tokens and paths, nothing a shell could reinterpret."""
        return ["--permission-mode", self.mode, "--settings", str(settings),
                "--append-system-prompt-file", str(prompt)]

    def evidence(self) -> dict:
        """What the run records, so a reader can see what its agents could do."""
        return {"schema": SCHEMA, "source": self.source, "digest": self.digest,
                "mode": self.mode, "deny": list(self.deny),
                "allow": {k: list(v) for k, v in self.allow.items()},
                "house_rules_sha256": hashlib.sha256(
                    self.house_rules.encode()).hexdigest()}


def parse(data: dict, source: str = "") -> RoleApprovalPolicy:
    """Validate a policy document. Refuses, with the reason, rather than
    applying a policy that is weaker than it looks."""
    if data.get("schema") != SCHEMA:
        raise PolicyRefused(f"schema is {data.get('schema')!r}, expected {SCHEMA!r}")
    mode = data.get("mode")
    if mode in REFUSED_MODES:
        raise PolicyRefused(f"mode {mode!r} {REFUSED_MODES[mode]}; a policy "
                            "exists so the run does not wait for a person")
    if mode not in MODES:
        raise PolicyRefused(f"mode {mode!r} is not one of {sorted(MODES)}")

    deny = tuple(data.get("deny") or ())
    missing = [d for d in MANDATORY_DENY if d not in deny]
    if missing:
        raise PolicyRefused(f"the deny-list is missing {len(missing)} mandatory "
                            f"entr{'y' if len(missing) == 1 else 'ies'}: "
                            + ", ".join(missing[:5]))

    house = (data.get("house_rules") or "").strip()
    lower = house.lower()
    absent = [p for p in _REQUIRED_PHRASES if p not in lower]
    if absent:
        raise PolicyRefused("the house-rules prompt must say " + ", ".join(
            repr(p) for p in absent) + "; it binds every session HoH spawns")

    allow: dict[str, tuple[str, ...]] = {}
    for role, rules in (data.get("roles") or {}).items():
        rules = tuple((rules or {}).get("allow") or ())
        for rule in rules:
            if _UNSCOPED.match(rule.strip()):
                raise PolicyRefused(f"{role}: allow rule {rule!r} is unscoped; "
                                    "name the command or the path")
            if rule in ("WebFetch", "WebSearch") or rule.startswith(("WebFetch(", "WebSearch(")):
                raise PolicyRefused(f"{role}: {rule!r} is network egress")
            if rule in deny:
                raise PolicyRefused(f"{role}: {rule!r} is both allowed and denied")
        allow[role] = rules

    canonical = json.dumps(data, sort_keys=True).encode()
    return RoleApprovalPolicy(mode=mode, deny=deny, house_rules=house, allow=allow,
                              source=source,
                              digest=hashlib.sha256(canonical).hexdigest()[:16])


def load(path: Path | str) -> RoleApprovalPolicy:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PolicyRefused(f"{p} cannot be read as a policy: {exc}") from exc
    return parse(data, source=str(p))
