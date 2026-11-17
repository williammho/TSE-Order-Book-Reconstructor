"""Load a TSE FLEX capture, predict auction prices, or replay a stock."""

import argparse
import gzip
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory


SCRIPTS = Path(__file__).resolve().parent


def run(script: str, *args: str | Path) -> None:
    subprocess.run([sys.executable, str(SCRIPTS / script), *map(str, args)], check=True)


def load(capture: Path, events: Path, max_frames: int | None) -> None:
    if not capture.is_file():
        raise FileNotFoundError(f"Capture not found: {capture}")
    if events.exists():
        raise FileExistsError(f"Choose a new events folder: {events}")
    events.parent.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(prefix=".tse-load-", dir=events.parent) as temp:
        tsv = Path(temp) / "capture.tsv"
        decoded = Path(temp) / "events"
        options = [] if max_frames is None else ["--max-frames", str(max_frames)]
        run("pcap.py", capture, tsv, *options)
        run("flex.py", tsv, decoded)
        tsv.unlink()

        stocks = sorted(decoded.glob("*.jsonl"))
        if not stocks:
            raise ValueError("The capture contains no decoded TSE FLEX stock events")
        print(f"Compressing {len(stocks):,} stock files…", flush=True)
        for stock in stocks:
            with stock.open("rb") as source, gzip.open(f"{stock}.gz", "wb", compresslevel=1) as dest:
                shutil.copyfileobj(source, dest)
            stock.unlink()
        decoded.rename(events)
    print(f"Events saved in {events}")


def predict(events: Path, reference: Path, output: Path,
            books: Path | None, actuals: Path | None) -> None:
    for source in (reference, actuals):
        if source is not None and not source.is_file():
            raise FileNotFoundError(f"CSV not found: {source}")
    if output.exists():
        raise FileExistsError(f"Choose a new results file: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    if books is None:
        books = events / "books_preclose"
        if not books.is_dir():
            with TemporaryDirectory(prefix=".tse-books-", dir=events) as temp:
                snapshots = Path(temp) / "books"
                run("orderbook.py", events, snapshots, "--time", "15:30:00")
                snapshots.rename(books)
    if not any(books.glob("*/metadata.json")):
        raise ValueError(f"No saved books found in {books}")

    options = [] if actuals is None else ["--actuals", actuals]
    run("itayose.py", "predict", books, reference, output, *options)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("load", help="Decode a PCAP into compressed stock files")
    prepare.add_argument("capture", type=Path)
    prepare.add_argument("events", type=Path, help="New folder for the decoded events")
    prepare.add_argument("--max-frames", type=int, help="Limit capture frames for a trial run")

    auction = commands.add_parser("predict", help="Predict closing auction prices for all stocks")
    auction.add_argument("events", type=Path)
    auction.add_argument("reference", type=Path, help="Trading day's stock reference CSV")
    auction.add_argument("output", type=Path, help="New results CSV")
    auction.add_argument("--books", type=Path, help="Existing 15:30 books from this capture")
    auction.add_argument("--actuals", type=Path, help="Existing actual-closes CSV for comparison")

    replay = commands.add_parser("replay", help="Show a snapshot or replay in a window")
    replay.add_argument("events", type=Path)
    replay.add_argument("--stock", required=True)
    replay.add_argument("--start", "--time", required=True, help="Start time in JST")
    replay.add_argument("--duration", help="Exchange seconds to replay; default is a snapshot")
    replay.add_argument("--speed", help="Playback multiplier, default 1")
    replay.add_argument("--interval", help="Screen refresh interval in seconds, default 0.05")
    replay.add_argument("--levels", help="Price levels per side, default 10")

    args = parser.parse_args()
    if args.command != "load" and not args.events.is_dir():
        parser.error(f"Events folder not found: {args.events}. Run load first.")

    try:
        if args.command == "load":
            load(args.capture, args.events, args.max_frames)
        elif args.command == "predict":
            predict(args.events, args.reference, args.output, args.books, args.actuals)
        else:
            options = ["--stock", args.stock, "--start", args.start]
            for name in ("duration", "speed", "interval", "levels"):
                value = getattr(args, name)
                if value is not None:
                    options.extend([f"--{name}", value])
            run("playback.py", args.events, *options)
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode if error.returncode > 0 else 1) from None
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
