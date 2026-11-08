""" Utilizes TShark to convert .pcap file with exchange messages into a .tsv file wiht raw bytes. .tsv file is then passed to FLEX.py for parsing bytes into order information.
Each TSV row contains an individual UDP, storing the frame number, timestamp, length, and UDP payload in hex.
"""

import argparse
from ipaddress import IPv4Address
from pathlib import Path
import shutil
import subprocess


PACKET_FIELDS = (
    "frame.number",
    "frame.time_epoch",
    "frame.cap_len",
    "frame.len",
    "udp.length",
    "udp.payload",
)

def export_udp_tsv(
    tshark: str,
    capture: Path,
    output: Path,
    *,
    max_frames: int | None = None,
) -> None:
    """ 
    Export UDP frames from PCAP to TSV. Constructs command to be run for TShark.
    """
    
    command = [
        tshark,
        "-n",
        "-r", str(capture),
        "-Y", "udp", #containing UDP
        "-T", "fields",
        "-E", "header=y", #include header
        "-E", "separator=/t", #separate with tab
        "-E", "occurrence=f", #take first occurence if multiple exis 
    ]

    if max_frames is not None:
        command.extend(["-c", str(max_frames)])

    for field in PACKET_FIELDS:
        command.extend(["-e", field])

    output_file = output.open("xb")
    try:
        with output_file:
            subprocess.run(command, stdout=output_file, check=True)
    except BaseException:
        output.unlink(missing_ok=True)
        raise

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export IPv4 UDP payloads from a capture to TSV."
    )
    parser.add_argument("capture", type=Path, help="Input PCAP file")
    parser.add_argument("output", type=Path, help="TSV output file")
    parser.add_argument("--max-frames", type=int, help="Maximum input frames to read")
    args = parser.parse_args()

    if args.max_frames is not None and args.max_frames < 1:
        parser.error("--max-frames must be positive")

    tshark = shutil.which("tshark")
    if tshark is None:
        parser.error("TShark not found")

    try:
        export_udp_tsv(
            tshark,
            args.capture,
            args.output,
            max_frames=args.max_frames
        )
    except (OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Export failed. {error}\n")


if __name__ == "__main__":
    main()