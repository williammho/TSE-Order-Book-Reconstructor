"""Open a TSE order-book snapshot or replay in a Matplotlib window.

Each run reconstructs from the selected stock's JSONL file. Display frames
are prepared before playback; the original book CSVs are not needed.
"""

import argparse
from bisect import bisect_right
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import gzip
import json
from pathlib import Path
from time import perf_counter

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from matplotlib.collections import PolyCollection
from matplotlib.patches import Rectangle
from tqdm import tqdm

from orderbook import (
    AFTERNOON_CLOSE, JST, MARKET_PRICE_RAW, MORNING_OPEN, PRE_CLOSING,
    PRICE_SCALE, SUPPORTED_CONDITIONS, BookSnapshot, Order,
    find_sources, session_at,
)


MICROSECONDS = 1_000_000
BACKGROUND = "#05070b"
PANEL = BACKGROUND
GRID = "#1b2029"
TEXT = "#cdd2da"
MUTED = "#68717e"
BID = "#00bf75"
ASK = "#ed3654"


@dataclass(frozen=True, slots=True)
class Level:
    price: int
    quantity: int
    orders: int


@dataclass(frozen=True, slots=True)
class Frame:
    stock: str
    at: datetime
    offset_us: int
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    market_buy: int
    market_sell: int
    last_price: int | None


def read_events(source: Path):
    """Read exchange timestamps, keeping continuation packets and deduping."""
    stock = source.name.split(".")[0]
    opener = gzip.open if source.suffix == ".gz" else open
    seen = set()
    frame = stream = last_sequence = last_update = second = None
    expected_index = 0
    skip = False
    with opener(source, "rt", encoding="utf-8") as file:
        for line_number, line in enumerate(file, 1):
            try:
                record = json.loads(line)
                if record["issue_code"] != stock:
                    raise ValueError("File contains another stock")
                if record["frame_number"] != frame:
                    packet_stream = record["multicast_group"], record["reboot_count"]
                    if stream is not None and packet_stream != stream:
                        raise ValueError("Stream or reboot count changed")
                    stream = packet_stream
                    sequence, update = record["sequence_number"], record["update_number"]
                    skip = sequence in seen
                    if not skip:
                        if last_sequence is not None and sequence < last_sequence:
                            raise ValueError("Packets are out of sequence")
                        if last_update is None and update != 1:
                            raise ValueError("File does not start at update 1")
                        if last_update is not None and not last_update <= update <= last_update + 1:
                            raise ValueError("Missing or reordered update")
                        seen.add(sequence)
                        last_sequence, last_update = sequence, update
                    frame = record["frame_number"]
                    expected_index = 0
                if skip:
                    continue
                if record["tag_index"] != expected_index:
                    raise ValueError("Missing or reordered tag")
                expected_index += 1

                tag = record["tag_type"]
                if tag == "T":
                    new_second = datetime.fromtimestamp(record["exchange_epoch_seconds"], JST)
                    if second is not None and (new_second.date() != second.date() or new_second < second):
                        raise ValueError("Exchange date changed or time moved backward")
                    second = new_second
                    event_time = second
                else:
                    if second is None:
                        raise ValueError("Event arrived before a T tag")
                    if tag not in {"A", "D", "E", "C", "O", "K"}:
                        raise ValueError(f"Unsupported tag {tag!r}")
                    micros = record.get("tag_time_raw")
                    if micros is None:
                        raw = bytes.fromhex(record["tag_hex"])
                        if len(raw) < 5:
                            raise ValueError("Tag is missing its timestamp")
                        micros = int.from_bytes(raw[1:5], "big")
                    if not isinstance(micros, int) or not 0 <= micros < MICROSECONDS:
                        raise ValueError("Invalid tag microseconds")
                    event_time = second.replace(microsecond=micros)
                yield event_time, record
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"{source.name}, line {line_number}: {error}") from error


class ReplayBook:
    """Mutable replay state, using orderbook.py's Order and snapshot rules."""

    def __init__(self, stock: str):
        self.stock = stock
        self.orders = {}
        self.opened_sessions = set()
        self.daily_base = None
        self.closing_reference = None
        self.last_execution = None
        self.last_o_tag_hex = None
        self.stream = None
        self.cursor = None

    def apply(self, record: dict, event_time: datetime):
        tag = record["tag_type"]
        if tag == "A":
            order_id = record["order_id"]
            side, quantity = record["side"], record["quantity"]
            condition, price = record["order_condition"], record["price_units"]
            if order_id in self.orders:
                raise ValueError(f"Duplicate live order {order_id}")
            if side not in {"B", "S"} or quantity <= 0 or price <= 0:
                raise ValueError("Invalid order side, quantity or price")
            if condition not in SUPPORTED_CONDITIONS:
                raise ValueError(f"Unsupported order condition {condition}")
            self.orders[order_id] = Order(
                order_id, side, None if price == MARKET_PRICE_RAW else price,
                quantity, condition, session_at(event_time),
            )
        elif tag == "D":
            order_id = record["order_id"]
            if order_id not in self.orders:
                raise ValueError(f"Delete references unknown order {order_id}")
            del self.orders[order_id]
        elif tag in {"E", "C"}:
            order_id = record["order_id"]
            order = self.orders.get(order_id)
            quantity = record["quantity"]
            if order is None:
                raise ValueError(f"Execution references unknown order {order_id}")
            if record["side"] != order.side or not 0 < quantity <= order.quantity:
                raise ValueError(f"Invalid execution for order {order_id}")
            if quantity == order.quantity:
                del self.orders[order_id]
            else:
                self.orders[order_id] = replace(order, quantity=order.quantity - quantity)
            self.opened_sessions.add(session_at(event_time))
            if tag == "C":
                self.last_execution = record["execution_price_units"]
        elif tag == "K":
            price = record["last_price_units"]
            if 0 < price < MARKET_PRICE_RAW:
                self.last_execution = price
        elif tag == "O":
            self.last_o_tag_hex = record["tag_hex"]
            raw_price = record["book_center_price_units"]
            price = raw_price if 0 < raw_price < MARKET_PRICE_RAW else None
            if event_time.time() < MORNING_OPEN and self.daily_base is None:
                self.daily_base = price
            if PRE_CLOSING <= event_time.time() < AFTERNOON_CLOSE:
                self.closing_reference = price
        self.cursor = record["sequence_number"], record["tag_index"]

    def snapshot(self, cutoff: datetime) -> BookSnapshot:
        return BookSnapshot(
            self.stock, cutoff, tuple(self.orders.values()),
            frozenset(self.opened_sessions), self.daily_base,
            self.closing_reference, self.last_execution, self.last_o_tag_hex,
            self.stream, self.cursor,
        )


def replay_snapshots(source: Path, cutoffs: list[time]):
    """Reconstruct once, yielding each exclusive cutoff before its next event."""
    if not cutoffs:
        return
    if any(b <= a for a, b in zip(cutoffs, cutoffs[1:])):
        raise ValueError("Display timestamps must increase")
    book = ReplayBook(source.name.split(".")[0])
    target = None
    index = 0
    for event_time, record in read_events(source):
        if target is None:
            target = datetime.combine(event_time.date(), cutoffs[0], tzinfo=JST)
            if target < event_time:
                raise ValueError("Requested start precedes the first exchange timestamp")
            book.stream = record["multicast_group"], record["reboot_count"]
        while event_time >= target:
            yield book.snapshot(target)
            index += 1
            if index == len(cutoffs):
                return
            target = datetime.combine(target.date(), cutoffs[index], tzinfo=JST)
        book.apply(record, event_time)
    raise ValueError(f"{source.name}: file does not cover the requested end time")


def display_times(start: time, duration: Decimal, speed: Decimal, interval: Decimal):
    """Keep refreshes in screen time; speed sets the exchange-time step."""
    if duration < 0 or speed <= 0 or interval <= 0:
        raise ValueError("Duration must be nonnegative; speed and interval must be positive")
    duration_us = duration * MICROSECONDS
    if duration_us != duration_us.to_integral_value():
        raise ValueError("Duration supports at most six decimal places")
    duration_us = int(duration_us)
    start_us = ((start.hour * 60 + start.minute) * 60 + start.second) * MICROSECONDS + start.microsecond
    if start_us + duration_us >= 86400 * MICROSECONDS:
        raise ValueError("A replay must stay within one trading date")
    step = speed * interval * MICROSECONDS
    if step < 1:
        raise ValueError("The exchange-time step must be at least one microsecond")
    offsets = [0]
    index = 1
    while True:
        offset = int(round(index * step))
        if offset >= duration_us:
            break
        offsets.append(offset)
        index += 1
    if duration_us:
        offsets.append(duration_us)
    origin = datetime.combine(datetime(2000, 1, 1).date(), start)
    return [(origin + timedelta(microseconds=n)).time() for n in offsets], offsets


def make_frame(snapshot: BookSnapshot, levels: int, offset_us: int) -> Frame:
    depth = {"B": {}, "S": {}}
    markets = {"B": 0, "S": 0}
    for order in snapshot.orders:
        included, price = snapshot.order_view(order)
        if not included:
            continue
        if price is None:
            markets[order.side] += order.quantity
        else:
            total = depth[order.side].setdefault(price, [0, 0])
            total[0] += order.quantity
            total[1] += 1
    bids = tuple(Level(p, *depth["B"][p]) for p in sorted(depth["B"], reverse=True)[:levels])
    asks = tuple(Level(p, *depth["S"][p]) for p in sorted(depth["S"])[:levels])
    return Frame(snapshot.stock, snapshot.cutoff, offset_us, bids, asks,
                 markets["B"], markets["S"], snapshot.last_execution_price_units)


def prepare_frames(source, start, duration, speed, interval, levels):
    cutoffs, offsets = display_times(start, duration, speed, interval)
    frames = []
    snapshots = replay_snapshots(source, cutoffs)
    try:
        with tqdm(total=len(cutoffs), desc="Preparing book", unit="frame") as progress:
            for offset, snapshot in zip(offsets, snapshots):
                frames.append(make_frame(snapshot, levels, offset))
                progress.update(1)
    finally:
        snapshots.close()
    return frames


def price_text(price: int) -> str:
    yen, fraction = divmod(price, PRICE_SCALE)
    return f"{yen:,}.{fraction:04d}".rstrip("0").rstrip(".")


def depth_points(levels, row_positions):
    """Cumulative quantity at the same vertical positions as the book rows."""
    points = []
    total = 0
    for level in levels:
        y = row_positions[level.price]
        if points:
            points.append((-total, y))
        total += level.quantity
        points.append((-total, y))
    return points


class BookWindow:
    def __init__(self, frames, levels, speed, interval):
        self.frames, self.levels = frames, levels
        self.speed, self.interval = float(speed), float(interval)
        self.offsets = [f.offset_us for f in frames]
        self.started = None
        self.animation = None
        with plt.rc_context({"toolbar": "None"}):
            self.fig = plt.figure(figsize=(8, 8.5), facecolor=BACKGROUND)
        self.fig.canvas.manager.set_window_title(frames[0].stock)
        self.depth_ax = self.fig.add_axes([0.075, 0.025, 0.405, 0.925], facecolor=PANEL)
        self.ladder_ax = self.fig.add_axes([0.48, 0.025, 0.505, 0.925], facecolor=PANEL,
                                           sharey=self.depth_ax)
        self.clock = self.fig.text(0.985, 0.975, "", ha="right", color=TEXT,
                                   fontsize=10, family="monospace")

        max_depth = max((sum(x.quantity for x in side) for frame in frames
                         for side in (frame.bids, frame.asks)), default=1)
        self.max_quantity = max((level.quantity for frame in frames for level in (*frame.bids, *frame.asks)), default=1)
        ax = self.depth_ax
        ax.set_xlim(-max(max_depth, 1) * 1.08, 0)
        ax.set_xticks([])
        ax.tick_params(colors=MUTED, labelsize=8, length=0, pad=5)
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.spines["right"].set_visible(True)
        ax.spines["right"].set_color(GRID)
        self.curves = {}
        self.fills = {}
        for side, color in (("B", BID), ("S", ASK)):
            self.curves[side], = ax.plot([], [], color=color, linewidth=1.2)
            self.fills[side] = PolyCollection([], facecolors=color, alpha=0.09, edgecolors="none")
            ax.add_collection(self.fills[side])

        ax = self.ladder_ax
        ax.set_xlim(0, 1)
        row_types = [("S", False)] * levels
        if any(frame.market_sell for frame in frames):
            row_types.append(("S", True))
        row_types.append((None, False))
        if any(frame.market_buy for frame in frames):
            row_types.append(("B", True))
        row_types.extend([("B", False)] * levels)
        ax.set_ylim(-0.55, len(row_types) - 0.45)
        ax.set_xticks([])
        ax.yaxis.set_visible(False)
        for spine in ax.spines.values():
            spine.set_visible(False)
        self.rows = []
        for index, (side, market) in enumerate(row_types):
            y = len(row_types) - 1 - index
            if side is None:
                self.middle = ax.text(0.5, y, "", ha="center", va="center", color=TEXT,
                                      fontsize=13, family="monospace")
                continue
            color = ASK if side == "S" else BID
            bar = Rectangle((0, y - 0.44), 0, 0.88, facecolor=color, alpha=0.15)
            ax.add_patch(bar)
            price = ax.text(0.025, y, "", color=color, va="center", fontsize=11, family="monospace")
            quantity = ax.text(0.97, y, "", color=TEXT, ha="right", va="center", fontsize=11, family="monospace")
            self.rows.append((side, market, bar, price, quantity))
        self.draw(0)

    def draw(self, index):
        frame = self.frames[index]
        self.clock.set_text(frame.at.strftime("%H:%M:%S.%f"))
        side_rows = {
            "S": iter([None] * (self.levels - len(frame.asks)) + list(reversed(frame.asks))),
            "B": iter(list(frame.bids) + [None] * (self.levels - len(frame.bids))),
        }
        row_positions = {"B": {}, "S": {}}
        price_ticks = []
        for side, market, bar, price, quantity in self.rows:
            if market:
                size = frame.market_sell if side == "S" else frame.market_buy
                label = "MKT" if size else ""
            else:
                level = next(side_rows[side])
                size = 0 if level is None else level.quantity
                label = "" if level is None else price_text(level.price)
                if level is not None:
                    y = price.get_position()[1]
                    row_positions[side][level.price] = y
                    price_ticks.append((y, label))
            bar.set_width(min(0.98, 0.98 * size / self.max_quantity))
            price.set_text(label)
            quantity.set_text(f"{size:,}" if size else "")
        for side, levels in (("B", frame.bids), ("S", frame.asks)):
            points = depth_points(levels, row_positions[side])
            self.curves[side].set_data([p[0] for p in points], [p[1] for p in points])
            polygon = [(0, points[0][1]), *points, (0, points[-1][1])] if points else []
            self.fills[side].set_verts([polygon] if polygon else [])
        if len(price_ticks) > 6:
            price_ticks = [price_ticks[round(i * (len(price_ticks) - 1) / 5)] for i in range(6)]
        self.depth_ax.set_yticks([y for y, _ in price_ticks],
                                labels=[label for _, label in price_ticks])
        if frame.last_price is not None:
            middle = price_text(frame.last_price)
        else:
            middle = ""
        self.middle.set_text(middle)
        return ()

    def advance(self, _):
        now = perf_counter()
        if self.started is None:
            self.started = now
        offset = int((now - self.started) * self.speed * MICROSECONDS)
        index = min(bisect_right(self.offsets, offset) - 1, len(self.frames) - 1)
        self.draw(index)
        if index == len(self.frames) - 1 and self.animation.event_source is not None:
            self.animation.event_source.stop()
        return ()

    def show(self):
        if len(self.frames) > 1:
            self.animation = FuncAnimation(
                self.fig, self.advance, interval=max(1, self.interval * 1000),
                init_func=lambda: (), repeat=False, cache_frame_data=False,
            )
        plt.show()


def parse_start(value):
    result = time.fromisoformat(value)
    if result.tzinfo is not None:
        raise argparse.ArgumentTypeError("Enter exchange time in JST without a timezone suffix")
    return result


def parse_number(value):
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError("Enter a finite number") from error
    if not result.is_finite():
        raise argparse.ArgumentTypeError("Enter a finite number")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="A stock JSONL/JSONL.GZ file or the events folder")
    parser.add_argument("--stock", help="Stock issue code, for example 7203")
    parser.add_argument("--start", "--time", type=parse_start, required=True,
                        help="Starting JST time, e.g. 15:00:00.123456")
    parser.add_argument("--duration", type=parse_number, default=Decimal(0),
                        help="Exchange seconds to replay; omit or use 0 for a snapshot")
    parser.add_argument("--speed", type=parse_number, default=Decimal(1), help="Playback multiplier, default 1")
    parser.add_argument("--interval", type=parse_number, default=Decimal("0.05"),
                        help="Screen refresh interval in seconds, default 0.05 (20 fps)")
    parser.add_argument("--levels", type=int, default=10, help="Displayed price levels per side, default 10")
    args = parser.parse_args()
    if args.levels < 1:
        parser.error("--levels must be positive")
    if args.source.is_dir() and not args.stock:
        parser.error("--stock is required when the source is a folder")
    try:
        source = find_sources(args.source, args.stock)[0]
        print(f"Reconstructing {source.name.split('.')[0]} from the event file…", flush=True)
        frames = prepare_frames(source, args.start, args.duration, args.speed, args.interval, args.levels)
        window = BookWindow(frames, args.levels, args.speed, args.interval)
        window.show()
    except (OSError, EOFError, KeyError, TypeError, ValueError) as error:
        parser.exit(1, f"Playback failed. {error}\n")


if __name__ == "__main__":
    main()
