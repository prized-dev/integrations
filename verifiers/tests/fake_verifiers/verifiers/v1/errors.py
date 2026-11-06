class RolloutError(Exception):
    """Base for errors that fail one rollout."""


class SandboxError(RolloutError):
    """A runtime/sandbox operation failed (provisioning, exec, or file I/O)."""
