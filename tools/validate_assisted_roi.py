from __future__ import annotations

import argparse
import platform
import sys
from tkinter import TclError
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.assisted_roi_checkpoint import CheckpointError, checkpoint_info
from tools.assisted_roi_gui import GuiContext, ValidationApp
from tools.assisted_roi_report import EnvironmentInfo, render_summary
from tools.assisted_roi_session import (
    SessionDocument,
    SessionFormatError,
    merge_session_items,
    metrics_for,
    read_session,
    write_session,
)
from tools.assisted_roi_validation import FrameOrder, discover_frames, order_frames
from tools.efficient_sam_predictor import EfficientSamPredictor


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline EfficientSAM-Ti reference-frame validation harness")
    parser.add_argument("--input", type=Path, default=Path("artifacts/reference-frames"))
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("artifacts/validation/assisted-roi"))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--verify-sha256", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--channel", type=int)
    parser.add_argument("--shuffle", action="store_true")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--device", choices=("cpu", "cuda", "auto"), default="cpu")
    return parser


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_error(category: str) -> int:
    messages = {
        "checkpoint_missing": "Checkpoint file was not found.",
        "checkpoint_unreadable": "Checkpoint file could not be read safely.",
        "checkpoint_sha256_mismatch": "Checkpoint SHA-256 does not match the recorded EfficientSAM-Ti checkpoint.",
        "session_invalid": "Validation session is invalid or unreadable.",
        "session_exists_use_resume": "A validation session already exists; use --resume or choose another output.",
        "no_frames": "No valid reference-frame artifacts were discovered.",
        "runtime_unavailable": "Optional EfficientSAM runtime or image dependencies are unavailable.",
        "gui_unavailable": "The native desktop validation window could not be started.",
        "unexpected_failure": "Validation harness could not start safely.",
    }
    print(f"Error: {messages.get(category, 'Validation harness could not start safely.')}")
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.limit is not None and args.limit < 1:
            return _safe_error("no_frames")
        print("Discovering reference frames...")
        checkpoint = checkpoint_info(args.checkpoint, args.verify_sha256)
        records = discover_frames(args.input)
        records = order_frames(
            records,
            FrameOrder(
                channel_id=args.channel,
                shuffle=args.shuffle,
                seed=args.seed,
                limit=args.limit,
            ),
        )
        if not records:
            return _safe_error("no_frames")
        session_path = args.output / "session.json"
        summary_path = args.output / "summary.md"
        if session_path.exists() and not args.resume:
            return _safe_error("session_exists_use_resume")
        existing = read_session(session_path) if args.resume and session_path.exists() else None
        items = merge_session_items(records, existing.items if existing else ())
        document = SessionDocument(
            checkpoint_name=checkpoint.name,
            expected_sha256=checkpoint.expected_sha256,
            actual_sha256=checkpoint.actual_sha256,
            device=args.device,
            items=items,
            updated_at_utc=_timestamp(),
        )
        args.output.mkdir(parents=True, exist_ok=True)
        write_session(session_path, document)
        environment = EnvironmentInfo(
            python_version=sys.version.split()[0],
            platform=platform.system(),
            device=args.device,
        )
        summary_path.write_text(
            render_summary(document, metrics_for(document.items), environment, args.output.as_posix()),
            encoding="utf-8",
        )
        print(f"Discovered {len(records)} reference frames.")
        print("Opening validation window...")
        print("Validation window ready. Click an object, classify the suggestion, and quit to save.")
        ValidationApp(
            GuiContext(
                records=records,
                document=document,
                predictor=EfficientSamPredictor(args.checkpoint, args.device),
                output_root=args.output,
                session_path=session_path,
                summary_path=summary_path,
                environment=environment,
            )
        ).run()
        print(f"Session saved: {session_path}")
        print(f"Summary saved: {summary_path}")
        return 0
    except CheckpointError as error:
        return _safe_error(error.category)
    except SessionFormatError as error:
        return _safe_error(error.reason)
    except (ImportError, ModuleNotFoundError):
        return _safe_error("runtime_unavailable")
    except (AttributeError, IndexError, KeyError, OSError, RuntimeError, TclError, TypeError, ValueError):
        return _safe_error("gui_unavailable")
    except Exception:
        return _safe_error("unexpected_failure")


if __name__ == "__main__":
    raise SystemExit(main())
