"""Allow `python -m tradingagents.evaluation` execution."""
import sys

from .cli import main

sys.exit(main())
