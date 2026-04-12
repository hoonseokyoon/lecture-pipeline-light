from codex_runner import identity
from _lib.pipeline import run_pipeline

normalize = identity()
expected_outputs = ["note.md"]
run = run_pipeline
