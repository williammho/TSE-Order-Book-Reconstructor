"""Reconstruct TSE order books from the per-stock .jsonl files written by flex.py.

The default cutoff is immediately before 15:30 JST. An earlier cutoff can be supplied. For example, 14:17 preserves the book
immediately before 14:17:00 JST.

Each stock produces compressed order-level and price-level CSV files,
plus metadata for the separate closing-auction calculator.
"""

import argparse
import csv
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta, timezone
import gzip
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from tqdm import tqdm


JST = timezone(timedelta(hours=9))
PRICE_SCALE = 10_000
MARKET_PRICE_RAW = (1 << 64) - 1

MORNING_OPEN = time(9)
MORNING_CLOSE = time(11, 30)
AFTERNOON_ACCEPTANCE = time(12, 5)
PRE_CLOSING = time(15, 25)
AFTERNOON_CLOSE = time(15, 30)

NORMAL = 0
ON_OPEN = 2
ON_CLOSE = 4
FUNARI = 6
SUPPORTED_CONDITIONS = {NORMAL, ON_OPEN, ON_CLOSE, FUNARI}


def session_at(exchange_time: datetime) -> str:
    if exchange_time.time() < AFTERNOON_ACCEPTANCE:
        return "morning"
    return "afternoon"


def format_price(price_units: int | None) -> str:
    """Format an integer FLEX price without floating-point arithmetic."""
    if price_units is None:
        return "MARKET"

    yen, fraction = divmod(price_units, PRICE_SCALE)
    return f"{yen}.{fraction:04d}"


@dataclass(frozen=True, slots=True)
class Order:
    order_id: int
    side: str
    price_units: int | None
    quantity: int
    condition: int
    session: str


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    stock: str
    cutoff: datetime
    orders: tuple[Order, ...]
    opened_sessions: frozenset[str]
    daily_base_price_units: int | None
    closing_reference_price_units: int | None
    last_execution_price_units: int | None
    last_o_tag_hex: str | None
    stream: tuple[int, int]
    last_cursor: tuple[int, int] | None

    def order_view(self, order: Order) -> tuple[bool, int | None]:
        """Return inclusion in depth and the order's effective price."""
        as_of = self.cutoff - timedelta(microseconds=1)
        clock = as_of.time()
        current_session = session_at(as_of)
        in_pre_closing = clock >= PRE_CLOSING

        included = True
        effective_price = order.price_units

        if order.condition == ON_OPEN:
            # Morning on-open orders can carry into the afternoon when
            # the morning session never established an opening price.
            original_open_pending = (
                order.session not in self.opened_sessions
            )
            current_open_pending = (
                current_session not in self.opened_sessions
            )
            included = original_open_pending and current_open_pending

        elif order.condition == ON_CLOSE:
            included = (
                order.session == "afternoon" and in_pre_closing
            )

        elif order.condition == FUNARI:
            if order.session == "morning":
                included = clock < MORNING_CLOSE
            elif in_pre_closing:
                effective_price = None

        return included, effective_price


def reconstruct_stock(
    source: Path,
    cutoff_time: time = AFTERNOON_CLOSE,
) -> BookSnapshot:
    """Replay one stock until the exclusive exchange-time cutoff.

    Continuation packets can omit the T tag. They inherit the exchange
    time established by the latest T tag in this stock's stream.
    """
    stock = source.name.split(".")[0]
    orders: dict[int, Order] = {}
    opened_sessions: set[str] = set()

    cutoff = None
    current_time = None
    stream = None
    last_frame = None
    last_sequence = None
    last_update = None
    last_cursor = None
    expected_tag_index = 0
    skip_packet = False
    reached_cutoff = False
    seen_sequences: set[int] = set()

    daily_base = None
    closing_reference = None
    last_execution = None
    last_o_tag_hex = None

    opener = gzip.open if source.suffix == ".gz" else open

    with opener(source, "rt", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            try:
                record = json.loads(line)

                if record["issue_code"] != stock:
                    raise ValueError("File contains another stock")

                frame = record["frame_number"]
                tag_type = record["tag_type"]

                if frame != last_frame:
                    if record["tag_index"] != 0:
                        raise ValueError(
                            "Expected packet to start at tag index 0"
                        )

                    packet_stream = (
                        record["multicast_group"],
                        record["reboot_count"],
                    )

                    if stream is None:
                        stream = packet_stream
                    elif packet_stream != stream:
                        raise ValueError(
                            "Stream or reboot count changed. "
                            "Replay requires recovery handling for this file."
                        )

                    sequence = record["sequence_number"]
                    update = record["update_number"]
                    skip_packet = sequence in seen_sequences

                    if not skip_packet:
                        if (
                            last_sequence is not None
                            and sequence < last_sequence
                        ):
                            raise ValueError("Packets are out of sequence")

                        if last_update is None:
                            if update != 1:
                                raise ValueError(
                                    "File does not start at update number 1"
                                )
                        elif not last_update <= update <= last_update + 1:
                            raise ValueError(
                                f"Unexpected update number "
                                f"{last_update} -> {update}"
                            )

                        # Multiple packets can share one update number.
                        # Group-wide sequence numbers need not be consecutive
                        # inside an individual stock's file.
                        seen_sequences.add(sequence)
                        last_sequence = sequence
                        last_update = update

                    last_frame = frame
                    expected_tag_index = 0

                if skip_packet:
                    continue

                if record["tag_index"] != expected_tag_index:
                    raise ValueError("Missing or reordered tag within packet")

                expected_tag_index += 1

                # Update the clock only when a T tag is present.
                # Continuation packets keep the previous exchange time.
                if tag_type == "T":
                    new_time = datetime.fromtimestamp(
                        record["exchange_epoch_seconds"], JST
                    )

                    if cutoff is None:
                        cutoff = datetime.combine(
                            new_time.date(), cutoff_time, tzinfo=JST
                        )

                    if new_time.date() != cutoff.date():
                        raise ValueError(
                            "File contains multiple trading dates"
                        )

                    if new_time >= cutoff:
                        reached_cutoff = True
                        break

                    current_time = new_time

                # Preserve the position of the last applied event.
                # The T tag at or beyond the cutoff is excluded.
                last_cursor = (
                    record["sequence_number"],
                    record["tag_index"],
                )

                if tag_type == "T":
                    continue

                if current_time is None:
                    raise ValueError(
                        "Order event arrived before exchange time"
                    )

                if tag_type == "A":
                    order_id = record["order_id"]
                    condition = record["order_condition"]
                    side = record["side"]
                    quantity = record["quantity"]
                    raw_price = record["price_units"]

                    if order_id in orders:
                        raise ValueError(
                            f"Duplicate live order ID {order_id}"
                        )
                    if condition not in SUPPORTED_CONDITIONS:
                        raise ValueError(
                            f"Unsupported order condition {condition}"
                        )
                    if side not in {"B", "S"} or quantity <= 0:
                        raise ValueError(
                            "Invalid side or order quantity"
                        )
                    if raw_price <= 0:
                        raise ValueError("Invalid order price")

                    price = raw_price
                    if raw_price == MARKET_PRICE_RAW:
                        price = None

                    orders[order_id] = Order(
                        order_id=order_id,
                        side=side,
                        price_units=price,
                        quantity=quantity,
                        condition=condition,
                        session=session_at(current_time),
                    )

                elif tag_type == "D":
                    order_id = record["order_id"]
                    if order_id not in orders:
                        raise ValueError(
                            f"Delete references unknown order {order_id}"
                        )
                    del orders[order_id]

                elif tag_type in {"E", "C"}:
                    order_id = record["order_id"]
                    order = orders.get(order_id)

                    if order is None:
                        raise ValueError(
                            f"Execution references unknown order {order_id}"
                        )

                    quantity = record["quantity"]
                    if record["side"] != order.side:
                        raise ValueError(
                            f"Execution side differs for order {order_id}"
                        )
                    if not 0 < quantity <= order.quantity:
                        raise ValueError(
                            f"Invalid execution quantity for order {order_id}"
                        )

                    remaining = order.quantity - quantity
                    if remaining == 0:
                        del orders[order_id]
                    else:
                        orders[order_id] = replace(
                            order, quantity=remaining
                        )

                    opened_sessions.add(session_at(current_time))

                    if tag_type == "C":
                        last_execution = record["execution_price_units"]

                elif tag_type == "K":
                    price = record["last_price_units"]
                    if 0 < price < MARKET_PRICE_RAW:
                        last_execution = price

                elif tag_type == "O":
                    last_o_tag_hex = record["tag_hex"]
                    price = record["book_center_price_units"]
                    reference = None
                    if 0 < price < MARKET_PRICE_RAW:
                        reference = price

                    clock = current_time.time()

                    if clock < MORNING_OPEN and daily_base is None:
                        daily_base = reference

                    if PRE_CLOSING <= clock < AFTERNOON_CLOSE:
                        closing_reference = reference

                else:
                    raise ValueError(
                        f"Unsupported tag type {tag_type!r}"
                    )

            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    f"{source.name}, line {line_number}: {error}"
                ) from error

    if cutoff is None or stream is None:
        raise ValueError(
            f"{source.name}: no events before the cutoff"
        )

    if not reached_cutoff:
        raise ValueError(
            f"{source.name}: file ended before the cutoff was confirmed"
        )

    return BookSnapshot(
        stock=stock,
        cutoff=cutoff,
        orders=tuple(orders.values()),
        opened_sessions=frozenset(opened_sessions),
        daily_base_price_units=daily_base,
        closing_reference_price_units=closing_reference,
        last_execution_price_units=last_execution,
        last_o_tag_hex=last_o_tag_hex,
        stream=stream,
        last_cursor=last_cursor,
    )


def write_snapshot(
    snapshot: BookSnapshot,
    output_dir: Path,
) -> None:
    """Publish a stock's files together after all three are written."""
    destination = output_dir / snapshot.stock

    if destination.exists():
        raise FileExistsError(
            f"Snapshot already exists: {destination}"
        )

    with TemporaryDirectory(
        prefix=f".{snapshot.stock}-", dir=output_dir
    ) as temporary:
        folder = Path(temporary)
        depth: dict[tuple[str, int | None], list[int]] = {}

        with gzip.open(
            folder / "orders.csv.gz",
            "xt",
            encoding="utf-8",
            newline="",
        ) as file:
            writer = csv.writer(file)
            writer.writerow([
                "order_id",
                "side",
                "remaining_quantity",
                "order_condition",
                "submitted_session",
                "submitted_price_units",
                "submitted_price",
                "included_in_depth",
                "effective_price_units",
                "effective_price",
            ])

            for order in snapshot.orders:
                included, effective_price = snapshot.order_view(order)

                writer.writerow([
                    order.order_id,
                    order.side,
                    order.quantity,
                    order.condition,
                    order.session,
                    order.price_units,
                    format_price(order.price_units),
                    int(included),
                    effective_price,
                    format_price(effective_price),
                ])

                if included:
                    totals = depth.setdefault(
                        (order.side, effective_price), [0, 0]
                    )
                    totals[0] += order.quantity
                    totals[1] += 1

        with gzip.open(
            folder / "depth.csv.gz",
            "xt",
            encoding="utf-8",
            newline="",
        ) as file:
            writer = csv.writer(file)
            writer.writerow([
                "side",
                "price_units",
                "price",
                "quantity",
                "order_count",
            ])

            for side in ("B", "S"):
                prices = sorted(
                    [
                        price
                        for level_side, price in depth
                        if level_side == side and price is not None
                    ],
                    reverse=(side == "B"),
                )

                # Market totals appear before the limit-price levels.
                for price in [None, *prices]:
                    level_totals = depth.get((side, price))
                    if level_totals is not None:
                        writer.writerow([
                            side,
                            price,
                            format_price(price),
                            level_totals[0],
                            level_totals[1],
                        ])

        metadata = {
            "stock": snapshot.stock,
            "cutoff_exclusive_jst": snapshot.cutoff.isoformat(),
            "time_precision": "one exchange second",
            "price_scale": PRICE_SCALE,
            "live_order_count": len(snapshot.orders),
            "opened_sessions": sorted(snapshot.opened_sessions),
            "daily_base_price_units": snapshot.daily_base_price_units,
            "closing_reference_price_units": (
                snapshot.closing_reference_price_units
            ),
            "last_execution_price_units": (
                snapshot.last_execution_price_units
            ),
            "last_o_tag_hex": snapshot.last_o_tag_hex,
            "multicast_group": snapshot.stream[0],
            "reboot_count": snapshot.stream[1],
            "last_sequence_and_tag_index": snapshot.last_cursor,
        }

        with (folder / "metadata.json").open(
            "x", encoding="utf-8"
        ) as file:
            json.dump(metadata, file, indent=2)
            file.write("\n")

        folder.rename(destination)


def find_sources(
    source: Path,
    stock: str | None,
) -> list[Path]:
    """Choose one input per stock, preferring gzip when both exist."""
    if source.is_file():
        if stock is not None and source.name.split(".")[0] != stock:
            raise ValueError(
                "--stock does not match the input file"
            )
        return [source]

    if not source.is_dir():
        raise ValueError(f"Input does not exist: {source}")

    paths: dict[str, Path] = {}
    for pattern in ("*.jsonl", "*.jsonl.gz"):
        for path in source.glob(pattern):
            paths[path.name.split(".")[0]] = path

    if stock is not None:
        if stock not in paths:
            raise ValueError(
                f"No input found for stock {stock}"
            )
        return [paths[stock]]

    if not paths:
        raise ValueError("No JSONL input files found")

    return [paths[symbol] for symbol in sorted(paths)]


def parse_cutoff(value: str) -> time:
    cutoff = time.fromisoformat(value)

    if cutoff.tzinfo is not None or cutoff.microsecond:
        raise argparse.ArgumentTypeError(
            "Use a whole-second JST time"
        )

    if not time(8) <= cutoff <= AFTERNOON_CLOSE:
        raise argparse.ArgumentTypeError(
            "Cutoff must be between 08:00 and 15:30"
        )

    return cutoff


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reconstruct TSE books from flex.py JSONL files."
    )
    parser.add_argument(
        "source",
        type=Path,
        help="One JSONL file or a folder of stock files",
    )
    parser.add_argument(
        "output_dir",
        type=Path,
        help="New folder for reconstructed books",
    )
    parser.add_argument(
        "--stock",
        help="Reconstruct only this stock, for example 7203",
    )
    parser.add_argument(
        "--time",
        type=parse_cutoff,
        default=AFTERNOON_CLOSE,
        help="Exclusive JST cutoff, default 15:30:00",
    )
    args = parser.parse_args()

    try:
        sources = find_sources(args.source, args.stock)
        args.output_dir.mkdir(parents=True, exist_ok=False)

        for source in tqdm(
            sources, desc="Reconstructing", unit="stock"
        ):
            snapshot = reconstruct_stock(source, args.time)
            write_snapshot(snapshot, args.output_dir)

    except (OSError, EOFError, ValueError) as error:
        parser.exit(1, f"Reconstruction failed. {error}\n")

    print(
        f"Wrote {len(sources):,} stock snapshots to {args.output_dir}"
    )


if __name__ == "__main__":
    main()