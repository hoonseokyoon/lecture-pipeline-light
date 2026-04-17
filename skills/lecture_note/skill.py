from codex_runner import identity
from _lib.pipeline import run_pipeline

normalize = identity()
expected_outputs = ["note.md", "compact.html"]
run = run_pipeline
