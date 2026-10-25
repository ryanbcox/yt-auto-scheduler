#!/usr/bin/env python3
"""Compatibility entry point for listing YouTube stream resource IDs."""
import sys

from schedule import main


if __name__ == "__main__":
    raise SystemExit(main(["list-streams", *sys.argv[1:]]))
