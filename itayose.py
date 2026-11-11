"""Calculate the closing auction price + volume from the books saved by orderbook.py.
"""

import argparse
import csv
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import gzip
import json
from pathlib import Path

from tqdm import tqdm


SCALE = 10_000
JST = timezone(timedelta(hours=9))
CLOSE = time(15, 30)
RULE_DATE = date(2024, 12, 25)

# Tick sizes as defined by TSE
TICKS = {
    "topix500": [
        (1_000, 1_000), (3_000, 5_000), (10_000, 10_000),
        (30_000, 50_000), (100_000, 100_000), (300_000, 500_000),
        (1_000_000, 1_000_000), (3_000_000, 5_000_000),
        (10_000_000, 10_000_000), (30_000_000, 50_000_000),
        (None, 100_000_000),
    ],
    "other": [
        (3_000, 10_000), (5_000, 50_000), (30_000, 100_000),
        (50_000, 500_000), (300_000, 1_000_000),
        (500_000, 5_000_000), (3_000_000, 10_000_000),
        (5_000_000, 50_000_000), (30_000_000, 100_000_000),
        (50_000_000, 500_000_000), (None, 1_000_000_000),
    ],
}
# One-unit ETFs cant have fractional price
TICKS["topix500_unit1"] = [
    (ceiling, max(tick, SCALE)) for ceiling, tick in TICKS["topix500"]
]

# Daily price limit (exclusive) - determined from prev day's closing price
DAILY_WIDTHS = [
    (100, 30), (200, 50), (500, 80), (700, 100), (1_000, 150),
    (1_500, 300), (2_000, 400), (3_000, 500), (5_000, 700),
    (7_000, 1_000), (10_000, 1_500), (15_000, 3_000),
    (20_000, 4_000), (30_000, 5_000), (50_000, 7_000),
    (70_000, 10_000), (100_000, 15_000), (150_000, 30_000),
    (200_000, 40_000), (300_000, 50_000), (500_000, 70_000),
    (700_000, 100_000), (1_000_000, 150_000),
    (1_500_000, 300_000), (2_000_000, 400_000),
    (3_000_000, 500_000), (5_000_000, 700_000),
    (7_000_000, 1_000_000), (10_000_000, 1_500_000),
    (15_000_000, 3_000_000), (20_000_000, 4_000_000),
    (30_000_000, 5_000_000), (50_000_000, 7_000_000),
    (None, 10_000_000),
]
#renewal width that determines the closing band during closing auction. Center price is the last executed price from continuous trading (Zaraba)
RENEWAL_WIDTHS = [
    (200, 5), (500, 8), (700, 10), (1_000, 15),
    (1_500, 30), (2_000, 40), (3_000, 50), (5_000, 70),
    (7_000, 100), (10_000, 150), (15_000, 300),
    (20_000, 400), (30_000, 500), (50_000, 700),
    (70_000, 1_000), (100_000, 1_500), (150_000, 3_000),
    (200_000, 4_000), (300_000, 5_000), (500_000, 7_000),
    (700_000, 10_000), (1_000_000, 15_000),
    (1_500_000, 30_000), (2_000_000, 40_000),
    (3_000_000, 50_000), (5_000_000, 70_000),
    (7_000_000, 100_000), (10_000_000, 150_000),
    (15_000_000, 300_000), (20_000_000, 400_000),
    (30_000_000, 500_000), (50_000_000, 700_000),
    (None, 1_000_000),
]

REFERENCE_FIELDS = [
    "stock", "date", "tick_table", "trading_unit", "base_price",
    "daily_lower", "daily_upper", "limits_source",
    "closing_lower", "closing_upper", "tie_reference",
    "special_allowed",
]

ACTUAL_FIELDS = [
    "stock", "date", "actual_price", "actual_price_units",
    "actual_volume", "buy_volume", "sell_volume", "actual_status", "detail",
]

RESULT_FIELDS = [
    "stock", "date", "predicted_price", "predicted_price_units",
    "predicted_volume", "method", "ticks_tested", "valid_candidates",
    "actual_price", "actual_volume", "price_match", "volume_match",
    "status", "detail",
]


class MissingReference(ValueError):
    """This stock needs reference information before it can be calculated."""


def to_units(value: str) -> int:
    """Convert a yen string to an exact integer price."""
    number = Decimal(value) * SCALE
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError(f"Invalid price: {value!r}")
    return int(number)


def price_text(price: int | None) -> str:
    if price is None:
        return ""
    return format(Decimal(price) / SCALE, "f")


def lookup_width(price: int, table: list) -> int:
    for upper, width in table:
        if upper is None or price < upper * SCALE:
            return width * SCALE
    raise ValueError("Price is outside the width table")


def valid_prices(lower: int, upper: int, table: str):
    """Produces every legal tick, including across tick-size changes."""
    previous_upper = 0
    for ceiling, tick in TICKS[table]:
        start = max(lower, previous_upper + 1)
        end = upper if ceiling is None else min(upper, ceiling * SCALE)
        first = ((start + tick - 1) // tick) * tick
        yield from range(first, end + 1, tick)
        if ceiling is None or ceiling * SCALE >= upper:
            break
        previous_upper = ceiling * SCALE


def read_csv(path: Path) -> dict[str, dict]:
    """Read a  CSV keyed by stock code. Note stock codes contain alphaets as well"""
    rows = {}
    with path.open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            stock = row["stock"].strip()
            if not stock or stock in rows:
                raise ValueError(f"Blank or repeated stock code in {path}")
            rows[stock] = row
    return rows


def read_metadata(folder: Path) -> dict:
    with (folder / "metadata.json").open(encoding="utf-8") as file:
        metadata = json.load(file)
    cutoff = datetime.fromisoformat(metadata["cutoff_exclusive_jst"])
    if cutoff.utcoffset() != timedelta(hours=9) or cutoff.time() != CLOSE:
        raise ValueError("Use a snapshot with the exclusive 15:30 JST cutoff")
    if cutoff.date() != RULE_DATE:
        raise ValueError("This first version is for 2024-12-25")
    if metadata["stock"] != folder.name or metadata["price_scale"] != SCALE:
        raise ValueError("Stock code or price scale disagrees with metadata")
    return metadata


def make_reference(books: Path, output: Path, instruments: Path | None) -> None:
    """Start a dated reference file using the saved snapshot metadata."""
    instrument_rows = read_csv(instruments) if instruments else {}
    folders = sorted(path.parent for path in books.glob("*/metadata.json"))
    if not folders:
        raise ValueError(f"No saved books found in {books}")

    with output.open("x", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=REFERENCE_FIELDS)
        writer.writeheader()
        for folder in tqdm(folders, desc="Reference", unit="stock"):
            metadata = read_metadata(folder)
            base = metadata.get("daily_base_price_units")
            info = instrument_rows.get(folder.name, {})
            row = {
                "stock": folder.name,
                "date": RULE_DATE.isoformat(),
                "tick_table": info.get("tick_table", ""),
                "trading_unit": info.get("trading_unit", ""),
                "base_price": price_text(base),
                "limits_source": "standard_table" if base else "",
                "special_allowed": "1" if metadata["opened_sessions"] else "",
            }
            if base:
                width = lookup_width(base, DAILY_WIDTHS)
                # The legal tick generator applies the positive-price floor.
                row["daily_lower"] = price_text(max(1, base - width))
                row["daily_upper"] = price_text(base + width)
            writer.writerow(row)
    print(f"Reference written to {output}")
    print("Fill tick_table and trading_unit where blank. Daily bounds can be edited.")


def read_book(folder: Path, reference: dict):
    """Load depth and the reference information for one stock."""
    metadata = read_metadata(folder)
    required = ("date", "tick_table", "trading_unit", "daily_lower", "daily_upper")
    missing = [name for name in required if not reference.get(name, "").strip()]
    if missing:
        raise MissingReference("Missing " + ", ".join(missing))
    if reference["date"] != RULE_DATE.isoformat():
        raise MissingReference("Reference date differs from the book date")
    table = reference["tick_table"].strip().lower()
    if table not in {"topix500", "other"}:
        raise MissingReference("tick_table must be topix500 or other")
    unit = int(reference["trading_unit"])
    if unit <= 0:
        raise MissingReference("trading_unit must be positive")
    if table == "topix500" and unit == 1:
        table = "topix500_unit1"

    lower = to_units(reference["daily_lower"])
    upper = to_units(reference["daily_upper"])
    if not 0 < lower <= upper:
        raise MissingReference("Invalid daily bounds")
    daily_ticks = list(valid_prices(lower, upper, table))
    if not daily_ticks:
        raise MissingReference("No legal ticks within the daily bounds")

    center = metadata.get("closing_reference_price_units")
    if reference.get("closing_lower") and reference.get("closing_upper"):
        band_lower = to_units(reference["closing_lower"])
        band_upper = to_units(reference["closing_upper"])
    elif center:
        width = 2 * lookup_width(center, RENEWAL_WIDTHS)
        band_lower, band_upper = center - width, center + width
    else:
        raise MissingReference("Missing closing-band reference")
    if band_lower > band_upper:
        raise MissingReference("Invalid closing band")

    bids, asks = {}, {}
    market_buy = market_sell = 0
    with gzip.open(folder / "depth.csv.gz", "rt", encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            side = row["side"]
            quantity = int(row["quantity"])
            if side not in {"B", "S"} or quantity <= 0:
                raise ValueError("Invalid depth side or quantity")
            if quantity % unit:
                raise MissingReference("Depth quantity disagrees with trading_unit")
            if row["price_units"] == "":
                if side == "B":
                    market_buy += quantity
                else:
                    market_sell += quantity
                continue
            price = int(row["price_units"])
            if not lower <= price <= upper:
                raise MissingReference("An order is outside daily bounds; check limits")
            if next(valid_prices(price, price, table), None) != price:
                raise MissingReference("An order is off tick; check tick_table")
            levels = bids if side == "B" else asks
            levels[price] = levels.get(price, 0) + quantity

    tie = metadata.get("last_execution_price_units")
    if reference.get("tie_reference"):
        tie = to_units(reference["tie_reference"])
    rules = {
        "unit": unit, "table": table,
        "daily_lower": daily_ticks[0], "daily_upper": daily_ticks[-1],
        "band_lower": band_lower, "band_upper": band_upper,
        "tie": tie, "special_allowed": reference.get("special_allowed", ""),
    }
    return bids, asks, market_buy, market_sell, rules


def ordinary_candidates(bids, asks, market_buy, market_sell, rules):
    """Test every tick using inclusive and strictly-better quantities to see if ordinary Itayose can be executed"""
    bid_prices = sorted(bids)
    ask_prices = sorted(asks)
    bid_index = ask_index = 0
    buy_total = market_buy + sum(bids.values())
    sell_better = market_sell
    candidates = []
    tested = 0

    for price in valid_prices(rules["daily_lower"], rules["daily_upper"], rules["table"]):
        tested += 1
        # Walking upward removes bids below p and adds asks below p.
        while bid_index < len(bid_prices) and bid_prices[bid_index] < price:
            buy_total -= bids[bid_prices[bid_index]]
            bid_index += 1
        while ask_index < len(ask_prices) and ask_prices[ask_index] < price:
            sell_better += asks[ask_prices[ask_index]]
            ask_index += 1

        buy_better = buy_total - bids.get(price, 0)
        sell_total = sell_better + asks.get(price, 0)
        volume = min(buy_total, sell_total)

        if volume < rules["unit"]:
            continue
        if volume < market_buy or volume < market_sell:
            continue
        if volume < buy_better or volume < sell_better:
            continue
        # Since volume is min(inclusive buy, inclusive sell), the smaller
        # side fills completely, including all its orders exactly at p.
        candidates.append((price, volume))

    return candidates, tested


def volume_at(price, bids, asks, market_buy, market_sell):
    """Return the eligible matched quantity at a fallback boundary."""
    buys = market_buy + sum(qty for p, qty in bids.items() if p >= price)
    sells = market_sell + sum(qty for p, qty in asks.items() if p <= price)
    return min(buys, sells)


def calculate_price(bids, asks, market_buy, market_sell, rules):
    """Try ordinary Itayose, stop allocation, then Special Execution."""
    candidates, tested = ordinary_candidates(bids, asks, market_buy, market_sell, rules)
    result = {
        "predicted_price": "", "predicted_price_units": "",
        "predicted_volume": 0, "method": "NO_EXECUTION",
        "ticks_tested": tested, "valid_candidates": len(candidates),
    }
    direction = None

    if candidates:
        if len(candidates) == 1:
            price, volume = candidates[0]
        else:
            if rules["tie"] is None:
                raise MissingReference("Several prices qualify; supply tie_reference")
            distance = min(abs(p - rules["tie"]) for p, _ in candidates)
            nearest = [(p, q) for p, q in candidates if abs(p - rules["tie"]) == distance]
            if len(nearest) != 1:
                raise MissingReference("Equidistant prices; check the exchange tie reference")
            price, volume = nearest[0]
        if rules["band_lower"] <= price <= rules["band_upper"]:
            result.update(predicted_price_units=price, predicted_volume=volume, method="ORDINARY")
        else:
            direction = "upper" if price > rules["band_upper"] else "lower"
    elif market_buy > market_sell + sum(asks.values()):
        direction = "upper"
    elif market_sell > market_buy + sum(bids.values()):
        direction = "lower"

    if direction:
        daily_price = rules[f"daily_{direction}"]
        if rules["band_lower"] <= daily_price <= rules["band_upper"]:
            price = daily_price
            method = "STOP_ALLOCATION"
        else:
            price = rules[f"band_{direction}"]
            method = "SPECIAL_EXECUTION"
            if rules["special_allowed"] not in {"0", "1"}:
                raise MissingReference("Confirm special_allowed for an issue with no opening")
            if rules["special_allowed"] == "0":
                return result

        if rules["daily_lower"] <= price <= rules["daily_upper"]:
            if next(valid_prices(price, price, rules["table"]), None) != price:
                raise MissingReference("Fallback boundary is off tick; supply the official closing bounds")
            volume = volume_at(price, bids, asks, market_buy, market_sell)
            if volume >= rules["unit"]:
                result.update(predicted_price_units=price, predicted_volume=volume, method=method)

    if result["predicted_price_units"] != "":
        result["predicted_price"] = price_text(result["predicted_price_units"])
    return result


def timed_events(source: Path):
    """Read one stock's events, remove repeated packets, and carry T time."""
    opener = gzip.open if source.suffix == ".gz" else open
    stock = source.name.split(".")[0]
    seen = set()
    frame = stream = last_sequence = last_update = current_time = None
    skip = False
    expected_index = 0
    with opener(source, "rt", encoding="utf-8") as file:
        for line_number, line in enumerate(file, 1):
            try:
                event = json.loads(line)
                if event["issue_code"] != stock:
                    raise ValueError("Unexpected stock code")
                if event["frame_number"] != frame:
                    packet_stream = (event["multicast_group"], event["reboot_count"])
                    if stream is not None and packet_stream != stream:
                        raise ValueError("Stream or reboot count changed")
                    stream = packet_stream
                    sequence, update = event["sequence_number"], event["update_number"]
                    skip = sequence in seen
                    if not skip:
                        if last_sequence is not None and sequence < last_sequence:
                            raise ValueError("Packets are out of sequence")
                        if last_update is None and update != 1:
                            raise ValueError("Event file does not start at update 1")
                        if last_update is not None and not last_update <= update <= last_update + 1:
                            raise ValueError("Missing or reordered update")
                        seen.add(sequence)
                        last_sequence, last_update = sequence, update
                    frame = event["frame_number"]
                    expected_index = 0
                if skip:
                    continue
                if event["tag_index"] != expected_index:
                    raise ValueError("Missing or reordered tag")
                expected_index += 1
                if event["tag_type"] == "T":
                    new_time = datetime.fromtimestamp(event["exchange_epoch_seconds"], JST)
                    if current_time is not None and new_time < current_time:
                        raise ValueError("Exchange time moved backward")
                    current_time = new_time
                if current_time is None:
                    raise ValueError("No exchange time before this event")
                if current_time.date() != RULE_DATE:
                    raise ValueError("Event date differs from 2024-12-25")
                yield current_time, event
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"{source.name}, line {line_number}: {error}") from error


def read_actual(source: Path) -> dict:
    """Extract the closing-auction price and one-sided executed volume."""
    buy_volume = sell_volume = 0
    price = auction_update = None
    reached_close = False
    for exchange_time, event in timed_events(source):
        if exchange_time.time() < CLOSE:
            continue
        reached_close = True
        if event["tag_type"] == "E":
            raise ValueError("Closing E tag has no explicit execution price; check this stock")
        if event["tag_type"] != "C":
            continue
        if auction_update is None:
            auction_update = event["update_number"]
        elif event["update_number"] != auction_update:
            raise ValueError("More than one execution update after the close")
        execution_price = event["execution_price_units"]
        if not 0 < execution_price < (1 << 64) - 1:
            raise ValueError("Invalid execution price")
        if price is not None and execution_price != price:
            raise ValueError("Closing executions contain several prices")
        price = execution_price
        quantity = event["quantity"]
        if quantity <= 0:
            raise ValueError("Invalid execution quantity")
        if event["side"] == "B":
            buy_volume += quantity
        elif event["side"] == "S":
            sell_volume += quantity
        else:
            raise ValueError("Invalid execution side")

    if not reached_close:
        raise ValueError("Event file does not reach the close")
    if buy_volume != sell_volume:
        raise ValueError(f"Buy/sell executed volume differs: {buy_volume}/{sell_volume}")
    return {
        "stock": source.name.split(".")[0], "date": RULE_DATE.isoformat(),
        "actual_price": price_text(price),
        "actual_price_units": "" if price is None else price,
        "actual_volume": buy_volume,
        "buy_volume": buy_volume, "sell_volume": sell_volume,
        "actual_status": "TRADE" if price is not None else "NO_RECORDED_AUCTION",
        "detail": "",
    }


def export_actuals(events: Path, output: Path, stock: str | None) -> None:
    sources = {}
    for pattern in ("*.jsonl", "*.jsonl.gz"):
        for path in events.glob(pattern):
            sources[path.name.split(".")[0]] = path
    if stock:
        sources = {stock: sources[stock]}
    if not sources:
        raise ValueError(f"No stock event files found in {events}")
    with output.open("x", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=ACTUAL_FIELDS)
        writer.writeheader()
        for code, source in tqdm(sorted(sources.items()), desc="Actual closes", unit="stock"):
            try:
                row = read_actual(source)
            except (OSError, KeyError, TypeError, ValueError) as error:
                row = {
                    "stock": code, "date": RULE_DATE.isoformat(),
                    "actual_status": "UNVERIFIABLE", "detail": str(error),
                }
            writer.writerow(row)
    print(f"Actual closing results written to {output}")


def predict_all(books: Path, reference_file: Path, output: Path,
                actual_file: Path | None, stock: str | None) -> None:
    references = read_csv(reference_file)
    actuals = read_csv(actual_file) if actual_file else {}
    folders = {p.parent.name: p.parent for p in books.glob("*/metadata.json")}
    codes = [stock] if stock else sorted(set(folders) | set(references) | set(actuals))
    if not codes:
        raise ValueError("No stocks found")
    counts = {}
    with output.open("x", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        for code in tqdm(codes, desc="Itayose", unit="stock"):
            row = {"stock": code, "date": RULE_DATE.isoformat(), "detail": ""}
            try:
                if code not in folders:
                    raise MissingReference("No saved book for this stock")
                if code not in references:
                    raise MissingReference("No reference row for this stock")
                book = read_book(folders[code], references[code])
                row.update(calculate_price(*book))
                row["status"] = "CALCULATED"
            except MissingReference as error:
                row.update(status="NEEDS_REFERENCE_DATA", detail=str(error))
            except (OSError, KeyError, TypeError, ValueError, ArithmeticError) as error:
                row.update(status="ERROR", detail=str(error))

            actual = actuals.get(code)
            if actual_file and row["status"] == "CALCULATED":
                row["status"] = "ACTUAL_UNAVAILABLE"
            if actual:
                row["actual_price"] = actual.get("actual_price", "")
                row["actual_volume"] = actual.get("actual_volume", "")
                if actual["date"] != RULE_DATE.isoformat():
                    row["detail"] += " Actual CSV has a different trading date."
                elif actual["actual_status"] == "UNVERIFIABLE":
                    row["detail"] += " Actual check: " + actual.get("detail", "")
                elif row["status"] in {"CALCULATED", "ACTUAL_UNAVAILABLE"}:
                    same_price = str(row["predicted_price_units"]) == actual["actual_price_units"]
                    same_volume = row["predicted_volume"] == int(actual["actual_volume"])
                    row.update(price_match=same_price, volume_match=same_volume)
                    if same_price and same_volume:
                        row["status"] = "MATCH" if actual["actual_status"] == "TRADE" else "NO_RECORDED_AUCTION"
                    else:
                        row["status"] = "MISMATCH"
            writer.writerow(row)
            counts[row["status"]] = counts.get(row["status"], 0) + 1
    print(f"Results written to {output}")
    print(", ".join(f"{status}: {count}" for status, count in sorted(counts.items())))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    reference = commands.add_parser("make-reference", help="Create the stock reference CSV")
    reference.add_argument("books", type=Path)
    reference.add_argument("output", type=Path)
    reference.add_argument("--instruments", type=Path,
                           help="Optional CSV: stock,tick_table,trading_unit")

    actual = commands.add_parser("actuals", help="Extract and cache actual auction results")
    actual.add_argument("events", type=Path)
    actual.add_argument("output", type=Path)
    actual.add_argument("--stock")

    prediction = commands.add_parser("predict", help="Calculate and optionally compare closes")
    prediction.add_argument("books", type=Path)
    prediction.add_argument("reference", type=Path)
    prediction.add_argument("output", type=Path)
    prediction.add_argument("--actuals", type=Path)
    prediction.add_argument("--stock")
    args = parser.parse_args()

    try:
        if args.command == "make-reference":
            make_reference(args.books, args.output, args.instruments)
        elif args.command == "actuals":
            export_actuals(args.events, args.output, args.stock)
        else:
            predict_all(args.books, args.reference, args.output, args.actuals, args.stock)
    except (OSError, KeyError, ValueError, ArithmeticError) as error:
        parser.exit(1, f"Itayose failed: {error}\n")


if __name__ == "__main__":
    main()
