# -*- coding: utf-8 -*-
"""
run.py -- Latinex Equity Tracker launcher.

Usage: python run.py
Opens the dashboard at http://localhost:8502
(port 8502 so it does not clash with the Taleb dashboard on 8501)
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    print("=" * 50)
    print("  LATINEX EQUITY TRACKER")
    print("  Panama Stock Exchange - equities only")
    print("  http://localhost:8502")
    print("=" * 50)
    subprocess.run(
        [sys.executable, "-m", "streamlit", "run",
         os.path.join(HERE, "dashboard.py"),
         "--server.port", "8502",
         "--server.headless", "true",
         "--browser.gatherUsageStats", "false"],
        cwd=HERE,
        check=False,
    )


if __name__ == "__main__":
    main()
