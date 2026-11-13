#!/usr/bin/env python3
"""启动迟到更正处理台。

用法: python3 run.py [--port 8000] [--db data/workbench.db]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server import app  # noqa: E402

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                "data", "workbench.db"))
    args = p.parse_args()
    os.makedirs(os.path.dirname(args.db), exist_ok=True)
    app.serve(args.db, args.port)
