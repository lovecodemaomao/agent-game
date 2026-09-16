#!/usr/bin/env python3
import logging
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python main3.py <port>')
    sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
    from coregeek_v2.server import serve
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s')
    serve(int(sys.argv[1]))


if __name__ == '__main__':
    main()
