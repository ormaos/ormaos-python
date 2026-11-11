"""Fixed I/O workload: a SQLite file and small text files in one directory.

2,000 inserts in 20 transactions, 200 queries, then 200 files written and
read back. The directory is the first argument; the bench gives a new, empty
one to every run. The same input every time, so the printed total is always
the same.
"""

import os
import sqlite3
import sys

BATCHES = 20
ROWS_PER_BATCH = 100
QUERIES = 200
FILES = 200


def store(conn, n):
    conn.execute("INSERT INTO items (n, label) VALUES (?, ?)", (n, f"item-{n}"))


def count_up_to(conn, n):
    row = conn.execute("SELECT count(*) FROM items WHERE n <= ?", (n,)).fetchone()
    return row[0]


def write_and_read(path):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("x" * 1000)
    with open(path, encoding="utf-8") as handle:
        return len(handle.read())


def main(directory):
    conn = sqlite3.connect(os.path.join(directory, "bench.db"))
    conn.execute("CREATE TABLE items (n INTEGER PRIMARY KEY, label TEXT)")
    for batch in range(BATCHES):
        with conn:
            for n in range(batch * ROWS_PER_BATCH, (batch + 1) * ROWS_PER_BATCH):
                store(conn, n)
    total = 0
    step = BATCHES * ROWS_PER_BATCH // QUERIES
    for n in range(0, BATCHES * ROWS_PER_BATCH, step):
        total += count_up_to(conn, n)
    conn.close()
    for n in range(FILES):
        total += write_and_read(os.path.join(directory, f"file-{n}.txt"))
    print(total)


if __name__ == "__main__":
    main(sys.argv[1])
