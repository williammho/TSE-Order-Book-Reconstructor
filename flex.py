r"""Decode the .tsv returnd from pcap.py into one JSONL file per TSE stock. Each row in the JSONL file corresponds to a individual tag (note: one TSE packet can contain multiple tags)

Uses MBO Multicast Groups 51 - 68 (for TSE). See TSE MBO specifications for information on header + payload format
"""

import argparse
import csv
import json
from pathlib import Path
import shutil
from tqdm import tqdm


TSE_GROUPS = range(51, 69)
HEADER_LENGTH = 26 #fixed length as defined by specs
WRITE_BATCH_SIZE = 64 #open a stock's file and write in batches of 64 records (optimization)
TSV_NUM_ROWS = 77_477_760

MIN_TAG_LENGTHS = {
    "T": 5, #time
    "A": 25, #add an order
    "D": 9, #delete an order
    "E": 16, #order executed
    "C": 29, #order executed with specified price 
    "O": 18, #trading status
    "K": 26, #execution summary 
}

REQUIRED_COLUMNS = {
    "frame.number",
    "frame.time_epoch",
    "udp.length",
    "udp.payload",
}


def read_uint(data: bytes, start: int, end: int) -> int:
    return int.from_bytes(data[start:end], byteorder="big")


def decode_tag(tag: bytes) -> dict[str, object]:
    """Decode an individal tag, starting at its type
    Format of a tag: tag[0] --> tag type, tag[1:n] --> tag info/payload (format is type-dependen)
    """
    tag_type = chr(tag[0])
    if len(tag) < MIN_TAG_LENGTHS.get(tag_type, 1):
        raise ValueError(f"{tag_type} tag is too short ({len(tag)} bytes)")

    fields: dict[str, object] = {
        "tag_type": tag_type,
        "tag_hex": tag.hex(),
    }

    if tag_type == "T":
        fields["exchange_epoch_seconds"] = read_uint(tag, 1, 5)
    elif tag_type == "O":
        fields["book_center_price_units"] = read_uint(tag, 10, 18)
    elif tag_type == "K":
        fields["last_price_units"] = read_uint(tag, 18, 26)
    elif tag_type in ("A", "D", "E", "C"):
        fields["tag_time_raw"] = read_uint(tag, 1, 5)
        fields["order_id"] = read_uint(tag, 5, 9)

        if tag_type in ("A", "E", "C"):
            fields["side"] = chr(tag[9])
            fields["quantity"] = read_uint(tag, 10, 16)

        if tag_type == "A":
            fields["price_units"] = read_uint(tag, 16, 24)
            fields["order_condition"] = tag[24]
        elif tag_type == "C":
            fields["execution_price_units"] = read_uint(tag, 20, 28)
            fields["pricing_method"] = tag[28]

    return fields


def decode_tse_packet(
    payload: bytes,
    frame_number: int,
    capture_time_epoch: str,
) -> list[dict[str, object]]:
    """Decode the tags in one packet.
    Format of a packet: header (fixed length) --- tag 1 --- tag 2 --- ... --- tag n (variable length)
    """

    if not payload or payload[0] not in TSE_GROUPS: #check if multicast group corresponds to a TSE #
        return []
    if len(payload) < HEADER_LENGTH:
        raise ValueError("Payload length is less than header lngth")

    issue_code = payload[6:18].strip().decode("ascii")
    if not issue_code:
        return []

    packet_fields = {
        "frame_number": frame_number,
        "capture_time_epoch": capture_time_epoch,
        "multicast_group": payload[0],
        "reboot_count": payload[1],
        "sequence_number": read_uint(payload, 2, 6),
        "issue_code": issue_code,
        "update_number": read_uint(payload, 18, 22),
    }

    records = []
    tag_count = payload[25] 
    offset = HEADER_LENGTH #start cursor right after header, and then advance tag-by-tag

    for tag_index in range(tag_count):
        if offset >= len(payload):
            raise ValueError(f"Missing length byte for tag {tag_index}")

        tag_length = payload[offset]
        start = offset + 1 
        end = start + tag_length
        if tag_length == 0 or end > len(payload):
            raise ValueError(f"Invalid length for tag {tag_index}")

        records.append({
            **packet_fields,
            "tag_index": tag_index,
            **decode_tag(payload[start:end]),
        })
        offset = end

    if offset != len(payload):
        raise ValueError("Unaccounted extra payload bytes")

    return records


def flush_stock(output_dir: Path, issue_code: str, lines: list[str]) -> None:
    """Open and add a batch of records (64) to one stock's file."""
    stock_path = output_dir / f"{issue_code}.jsonl"
    with stock_path.open("a", encoding="utf-8") as stock_file:
        stock_file.writelines(lines)
    lines.clear()


def export_tse_events(capture_tsv: Path, output_dir: Path) -> tuple[int, int]:
    """Write one JSONL file per stock
    For each row in the TSV file: 1. parse by calling decode_tse_packet 2. write to JSONL for corresponding stock (in batches of 64)
    """

    record_count = 0
    pending: dict[str, list[str]] = {} #set up buffer for each stock 

    with capture_tsv.open(encoding="utf-8", newline="") as source:
        rows = csv.DictReader(source, delimiter="\t")

        output_dir.mkdir(parents=True, exist_ok=False)
        try:
            for row in tqdm(rows, desc="Reading TSV", unit="row", unit_scale=True, total=TSV_NUM_ROWS): #change total based on dataset  
                try:
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError("Incorrect number of TSV columns")

                    payload = bytes.fromhex(row["udp.payload"])
                    if len(payload) + 8 != int(row["udp.length"]): #8 bytes of fixed UDP header 
                        raise ValueError("Payload size disagrees with UDP length")

                    records = decode_tse_packet(
                        payload,
                        int(row["frame.number"]),
                        row["frame.time_epoch"],
                    )
                except ValueError as error:
                    raise ValueError(f"TSV line {rows.line_num}. {error}") from error

                if not records:
                    continue

                issue_code = str(records[0]["issue_code"])
                lines = pending.setdefault(issue_code, [])
                lines.extend(json.dumps(record) + "\n" for record in records)
                record_count += len(records)

                if len(lines) >= WRITE_BATCH_SIZE:
                    flush_stock(output_dir, issue_code, lines)

            for issue_code, lines in pending.items():
                if lines:
                    flush_stock(output_dir, issue_code, lines)
        except BaseException:
            shutil.rmtree(output_dir)
            raise

    return record_count, len(pending)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export .tsv to one JSONL file per TSE stock."
    )
    parser.add_argument("capture_tsv", type=Path, help="Input TSV returned from pcap.py")
    parser.add_argument("output_dir", type=Path, help="New folder for JSONL files")
    args = parser.parse_args()

    try:
        record_count, stock_count = export_tse_events(
            args.capture_tsv,
            args.output_dir,
        )
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(1, f"Decode failed. {error}\n")

    print(f"Wrote {record_count:,} records across {stock_count:,} files to folder: {args.output_dir}")


if __name__ == "__main__":
    main()