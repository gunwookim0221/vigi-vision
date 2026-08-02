from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

EXPECTED_EFFICIENT_SAM_TINY_SHA256 = "dff858b19600a46461cbb7de98f796b23a7a888d9f5e34c0b033f7d6eb9e4e6a"


@dataclass(frozen=True, slots=True)
class CheckpointInfo:
    name: str
    expected_sha256: str
    actual_sha256: str | None


@dataclass(frozen=True, slots=True)
class CheckpointError(Exception):
    category: str

    def __str__(self) -> str:
        return self.category


def checkpoint_info(path: Path, verify_sha256: bool) -> CheckpointInfo:
    if not path.is_file():
        raise CheckpointError("checkpoint_missing")
    actual = None
    if verify_sha256:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as error:
            raise CheckpointError("checkpoint_unreadable") from error
        actual = digest.hexdigest()
        if actual != EXPECTED_EFFICIENT_SAM_TINY_SHA256:
            raise CheckpointError("checkpoint_sha256_mismatch")
    return CheckpointInfo(
        name=path.name,
        expected_sha256=EXPECTED_EFFICIENT_SAM_TINY_SHA256,
        actual_sha256=actual,
    )
