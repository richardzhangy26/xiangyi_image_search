"""T13's deliberately unavailable formal-deletion capability seam.

Only T14 may replace this source in a worker composition root after separate
human authorization and OSS trust-boundary verification.
"""


class UnavailableFormalDeletionCapabilitySource:
    def evaluate(self) -> bool:
        return False
