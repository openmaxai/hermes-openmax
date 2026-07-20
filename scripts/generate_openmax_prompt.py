#!/usr/bin/env python3
"""Print a copyable Hermes/OpenMax onboarding prompt from this checkout."""

from pathlib import Path
import sys

# Prefer the source tree containing this script over any older installed wheel.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_openmax.prompt import main  # noqa: E402


if __name__ == "__main__":
    main()
