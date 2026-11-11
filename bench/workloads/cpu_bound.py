"""Fixed CPU-bound workload: 150,000 calls of small pure-Python functions.

No I/O apart from one line printed at the end. The same input every time, so
the printed checksum is always the same.
"""

ROUNDS = 50_000
TABLE = list(range(256))


def mix(n):
    value = (n * 2654435761) % 4294967296
    value ^= value >> 13
    return value % 1009


def score(n):
    total = mix(n)
    total += sum(TABLE[n % 128 :])
    return total + mix(total)


def main():
    checksum = 0
    for n in range(ROUNDS):
        checksum = (checksum + score(n)) % 1000003
    print(checksum)


if __name__ == "__main__":
    main()
