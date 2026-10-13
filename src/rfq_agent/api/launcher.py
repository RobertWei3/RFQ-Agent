"""Worker processes the API starts for runs and reviews (spec 13, 15.1)."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Protocol


class Launcher(Protocol):
    """Starts ``rfq <argv...>`` in the background, output appended to ``log_path``."""

    def launch(self, argv: list[str], log_path: Path) -> None: ...


class SubprocessLauncher:
    """A detached ``python -m rfq_agent.cli`` process: it outlives an API restart."""

    def __init__(self, config_dir: Path) -> None:
        self._env = {**os.environ, "RFQ_CONFIG_DIR": str(config_dir)}

    def launch(self, argv: list[str], log_path: Path) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "rfq_agent.cli", *argv],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=self._env,
                start_new_session=True,
            )
        # Reap the worker when it exits, so it does not linger as a zombie.
        threading.Thread(target=process.wait, daemon=True).start()
