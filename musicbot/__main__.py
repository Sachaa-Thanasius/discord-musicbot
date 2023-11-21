from __future__ import annotations

import os

from ._main import main


os.umask(0o077)
raise SystemExit(main())
