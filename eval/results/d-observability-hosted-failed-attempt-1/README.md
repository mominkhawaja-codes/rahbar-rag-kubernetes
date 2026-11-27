# FAILED attempt 1: not evidence

Run on 27 Nov 2026 with scripts/d_trace_demo_hosted.sh from a Git Bash started by PowerShell.
Root cause: the script called plain `python`, which there was the system Python (no uvicorn),
not the repository's .venv. api-stdout.log shows 'No module named uvicorn'; the API never started,
so every request failed (empty responses) and no trace was written. The script did not stop on
the failure and printed 'done'. Replaced by scripts/d_trace_demo_hosted.py. Kept only as a record.

(The one line of api-stdout.log originally began with the full path of the system Python, which contained the developer's Windows user name; that path is replaced by <system Python>. Nothing else was changed.)
