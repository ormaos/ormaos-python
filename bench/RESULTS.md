# Recording overhead

Written by `bench/run.py`. Each row is the whole command, from start to exit, in milliseconds of wall-clock time: for `ormaos run` that includes starting the CLI, running the program, and reading the recording to print the summary.

- N: 20 runs of each row, after 1 discarded; the rows take turns
- Machine: AMD Ryzen 7 7735HS with Radeon Graphics, 16 logical CPUs, x86_64
- OS: Linux-7.0.0-43-generic-x86_64-with-glibc2.43
- Python: CPython 3.13.17
- Ormaos: 0.0.1
- Temporary directory: /tmp
- When: 2026-11-17 16:47 -03

## CPU-bound workload

`bench/workloads/cpu_bound.py`: 150,000 calls of small pure-Python functions, no I/O.

| Capability set | Command | Median (ms) | p25-p75 (ms) | Min-max (ms) | Added to the median | Recorded in one run |
|---|---|---:|---:|---:|---:|---|
| without Ormaos | `python` | 81.7 | 80.2-82.5 | 79.1-85.3 |  | nothing |
| context only | `ormaos run -- env -u ORMAOS_DIR python` | 126.0 | 124.3-128.0 | 121.0-130.6 | 44.3 (x1.54) | the context of the command |
| calls and exceptions | `ormaos run --no-io -- python` | 2415.4 | 2406.6-2438.0 | 2351.3-2453.1 | 2333.7 (x29.56) | 150,002 calls, 0 exceptions, 0 io events |
| calls, exceptions and path | `ormaos run --no-io --path cpu_bound.py:score -- python` | 2599.3 | 2584.5-2610.2 | 2533.0-2624.0 | 2517.6 (x31.81) | 150,002 calls, 0 exceptions, 150,000 lines, 0 io events |
| calls, exceptions and io (the default) | `ormaos run -- python` | 2666.2 | 2651.0-2677.5 | 2599.3-2692.3 | 2584.5 (x32.63) | 150,002 calls, 0 exceptions, 0 io events |

Listed as not collected in these recordings:
- context only: everything inside Python: no Python process reported: the command started no Python, or a Python without ormaos installed in its environment

## I/O workload

`bench/workloads/io_bound.py`: a SQLite file (2,000 inserts in 20 transactions, 200 queries) and 200 small files written and read back.

| Capability set | Command | Median (ms) | p25-p75 (ms) | Min-max (ms) | Added to the median | Recorded in one run |
|---|---|---:|---:|---:|---:|---|
| without Ormaos | `python` | 33.1 | 32.8-34.2 | 32.5-35.2 |  | nothing |
| context only | `ormaos run -- env -u ORMAOS_DIR python` | 76.0 | 75.9-77.3 | 75.2-79.8 | 42.9 (x2.29) | the context of the command |
| calls and exceptions | `ormaos run --no-io -- python` | 125.1 | 124.4-126.4 | 122.8-134.2 | 92.0 (x3.77) | 2,402 calls, 0 exceptions, 0 io events |
| calls, exceptions and path | `ormaos run --no-io --path io_bound.py:store -- python` | 127.4 | 126.5-128.1 | 124.8-132.3 | 94.3 (x3.84) | 2,402 calls, 0 exceptions, 2,000 lines, 0 io events |
| calls, exceptions and io (the default) | `ormaos run -- python` | 222.6 | 221.4-223.8 | 219.5-224.7 | 189.4 (x6.71) | 2,402 calls, 0 exceptions, 3,042 io events |

Listed as not collected in these recordings:
- context only: everything inside Python: no Python process reported: the command started no Python, or a Python without ormaos installed in its environment

## How to read it

- "Added to the median" is the median of the row minus the median of "without Ormaos", and their ratio.
- "context only" is `ormaos run` around a Python that starts without the recorder: the cost of the command itself, with nothing recorded inside Python.
- The other rows record inside Python. Their cost grows with the number of calls, lines and I/O events, shown in the last column, and with the time to read the recording back for the summary.
- These are two workloads on one machine. Another program has another number of calls per second, and another cost.
