"""Account handoff coordinator for a future macOS/VS Code frontend adapter.

No production desktop adapter is included. This module never manipulates auth
files, launches apps, or approves actions. See README.md for integration limits.
"""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import tempfile
from typing import List, Optional, Protocol


class RotationStopped(RuntimeError):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    identity: str  # Opaque identity supplied by an authenticated adapter, not a token.


@dataclass(frozen=True)
class Usage:
    windows: List[float]
    observed_at: float
    blocked: bool = False

    def eligible(self, now, threshold=92, max_age=60):
        return (not self.blocked and 0 <= now - self.observed_at <= max_age
                and bool(self.windows)
                and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                        and math.isfinite(v) and 0 <= v < threshold for v in self.windows))


@dataclass(frozen=True)
class Thread:
    frontend: str
    id: str
    project: str


@dataclass(frozen=True)
class Handoff:
    thread: Thread
    markdown: str
    work_incomplete: bool
    safe_to_continue: bool


class Adapter(Protocol):
    """Implement with an explicitly supported, authenticated integration.

    quiesce must prevent NEW turns across ALL managed frontends and drain active
    tools, then return the frozen thread inventory. This lock must survive account
    changes until release. A simple polling check or filesystem swap is insufficient.
    Calls must have bounded timeouts. Exceptions must not contain credentials.
    """
    def usage(self, profile: Profile) -> Usage: ...
    def quiesce(self) -> List[Thread]: ...
    def summarize(self, thread: Thread) -> Handoff: ...
    def activate(self, profile: Profile) -> None: ...
    def verify(self, profile: Profile) -> bool: ...
    def import_handoff(self, profile: Profile, handoff: Handoff) -> str: ...
    def release(self) -> None: ...


class Coordinator:
    """Two-phase switch. Stop and retain the journal on any ambiguous outcome.

    Importing a handoff must only prepare a draft/new chat. It must not execute
    project commands or replay the original task. Automatic continuation requires
    a separate verified frontend implementation and is intentionally absent.
    """
    def __init__(self, adapter: Adapter, profiles, active: Profile, directory,
                 clock, threshold=92, entry_margin=10):
        if not 1 <= threshold <= 99:
            raise ValueError("threshold must be 1..99")
        if not 0 <= entry_margin <= 20:
            raise ValueError("entry_margin must be 0..20")
        if len({p.identity for p in profiles}) != len(profiles):
            raise ValueError("profiles must represent distinct accounts")
        if active not in profiles:
            raise ValueError("active profile must be configured")
        self.adapter, self.profiles, self.active = adapter, profiles, active
        self.directory, self.clock, self.threshold = Path(directory), clock, threshold
        self.entry_threshold = max(1, threshold - entry_margin)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.journal = self.directory / "transaction.json"

    def write(self, name, data):
        fd, path = tempfile.mkstemp(dir=self.directory, prefix=".pending-")
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(data, out, ensure_ascii=False, indent=2)
                out.flush()
                os.fsync(out.fileno())
            os.replace(path, self.directory / name)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def rotate_if_needed(self):
        # One process-wide coordinator must own both frontend adapters. The journal
        # is a crash barrier, not a replacement for an interprocess deployment lock.
        if self.journal.exists():
            raise RotationStopped("Unresolved prior transaction: manual recovery required")
        if self.adapter.usage(self.active).eligible(self.clock(), self.threshold):
            return None
        available = []
        for profile in self.profiles:
            if profile.identity == self.active.identity:
                continue
            try:
                usage = self.adapter.usage(profile)
                usable = usage.eligible(self.clock(), self.entry_threshold)
            except Exception:
                continue
            if usable:
                available.append((max(usage.windows), profile.identity, profile))
        target = min(available, key=lambda item: (item[0], item[1]))[2] if available else None
        if target is None:
            raise RotationStopped("No account has verified fresh quota; wait for reset")
        record = {"phase": "preparing", "from": self.active.name, "to": target.name,
                  "threads": [], "imports": []}
        self.write("transaction.json", record)
        switched = False
        source = self.active
        try:
            threads = self.adapter.quiesce()
            if len({(t.frontend, t.id) for t in threads}) != len(threads):
                raise RotationStopped("Ambiguous thread inventory")
            summaries = []
            for index, thread in enumerate(threads):
                handoff = self.adapter.summarize(thread)
                if (handoff.thread != thread or not handoff.markdown.strip()
                        or not handoff.safe_to_continue):
                    raise RotationStopped("Missing or unsafe handoff; account unchanged")
                filename = "handoff-%03d.json" % index
                self.write(filename, {"frontend": thread.frontend, "thread": thread.id,
                                      "project": thread.project,
                                      "markdown": handoff.markdown,
                                      "work_incomplete": handoff.work_incomplete})
                record["threads"].append(filename)
                self.write("transaction.json", record)
                summaries.append(handoff)
            # Revalidate after summary costs and any time spent draining tools.
            if not self.adapter.usage(target).eligible(self.clock(), self.entry_threshold):
                raise RotationStopped("Target quota changed during checkpoint")
            record["phase"] = "activating"
            self.write("transaction.json", record)
            # Activation can partially succeed before throwing. Always roll back
            # after calling it, even when it never returned successfully.
            switched = True
            self.adapter.activate(target)
            if not self.adapter.verify(target):
                raise RotationStopped("Frontends disagree about active account")
            record["phase"] = "importing"
            self.write("transaction.json", record)
            for handoff in summaries:
                new_id = self.adapter.import_handoff(target, handoff)
                if not new_id:
                    raise RotationStopped("Handoff import not acknowledged")
                record["imports"].append({"frontend": handoff.thread.frontend, "id": new_id})
                self.write("transaction.json", record)
            self.active = target
            record["phase"] = "complete"
            self.write("last-transaction.json", record)
            self.journal.unlink()
            self.adapter.release()
            return target
        except Exception as exc:
            if switched:
                try:
                    self.adapter.activate(source)
                    if not self.adapter.verify(source):
                        raise RotationStopped("Rollback identity verification failed")
                    self.active = source
                    record["phase"] = "rolled_back"
                except Exception:
                    record["phase"] = "recovery_required"
            else:
                record["phase"] = "checkpoint_failed"
            self.write("transaction.json", record)
            # Keep both frontends paused on failure; no unattended task replay.
            raise RotationStopped("Switch stopped; inspect transaction and handoffs") from exc
