"""CLI: python -m schemagate --host 127.0.0.1 --port 5212"""
import argparse

from .server import serve


def main():
    parser = argparse.ArgumentParser(prog="schemagate")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5212)
    parser.add_argument("--db", default="schemagate.db")
    args = parser.parse_args()
    serve(args.host, args.port, args.db)


if __name__ == "__main__":
    main()
