"""Read an exchange rate from the local archive."""

import argparse
import json

# Millionths, as the archive stores them.
RATES = {"XYZ": 735190, "ABC": 1104420}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    args = parser.parse_args()
    value = RATES[args.symbol]
    print(json.dumps({"symbol": args.symbol, "rate": f"{value / 1_000_000:.5f}"}))


if __name__ == "__main__":
    main()
