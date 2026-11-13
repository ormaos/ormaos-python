"""Execute a notebook in a real Jupyter kernel (nbclient and ipykernel) and
print what its cells printed."""

import sys

import nbformat
from nbclient import NotebookClient

notebook = nbformat.read(sys.argv[1], as_version=4)
NotebookClient(notebook, kernel_name="python3", timeout=120).execute()
for cell in notebook.cells:
    for output in cell.get("outputs", []):
        if output.get("output_type") == "stream":
            print(output["text"], end="")
        elif output.get("output_type") == "error":
            print("error", output.get("ename"))
