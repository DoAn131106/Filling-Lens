from pathlib import Path
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent
TABLE_DIR = PROJECT_ROOT / "data" / "parsed" / "tables"


def inspect_tables(max_tables=20):
    table_files = list(TABLE_DIR.glob("*.csv"))

    if not table_files:
        print("No table CSV files found.")
        return

    for i, path in enumerate(table_files[:max_tables]):
        print("=" * 80)
        print(f"TABLE {i}: {path.name}")
        print("=" * 80)

        try:
            df = pd.read_csv(path)
            print(df.head(10))
            print()
            print(f"Shape: {df.shape}")
        except Exception as e:
            print(f"Could not read table: {e}")

        print()


if __name__ == "__main__":
    inspect_tables(max_tables=20)