# Try freeze on sample data

`demo_data.py` makes a folder of daily order exports, so you can watch a recording turn into a
workflow and then see it handle a new file, a missing folder and an edited report. This is the
setup used for the demo video.

## Setup

```bash
hermes plugins install https://github.com/mauricemohr88-debug/hermes-freeze && hermes plugins enable freeze
python3 demo/demo_data.py setup ~/freeze-demo    # from a clone of this repo: five CSVs, Nov 2 to 6
cd ~/freeze-demo
```

Hermes 0.23.0 or later. The video was recorded with `gpt-6.1-sol` (openai-codex) on v0.23.0.

## 1. Let Hermes do it once

Start `hermes` in that folder and give it the task:

> Every morning I need this: read each CSV in ./data with your file tools (no scripts) and count
> the rows whose status is 'refunded'. Write ./refunds.md with one line per file,
> '<filename>: <count>', sorted by filename, and tell me which day had the most refunds.

Expect about 17 s, 5 model calls and 58K tokens. Nov 3 has the most refunds (4).

Keep "with your file tools (no scripts)". Without it, the model may write an inline Python
script. That still freezes, but Hermes flags the script, so every replay asks for approval and
`schedule` refuses. With file tools you get a loop over the files and a workflow that runs
unattended. Weaker models sometimes skip files and guess their numbers; `/freeze` then refuses
and names the files whose data it never saw.

## 2. Freeze it

Type `/freeze refunds` (paste it, or type it slowly: fast typing can let autocomplete eat the
space). Expect 7 steps: a file search, `read_file` for each item `[was 5 calls]`, two more
searches, compiled report code, `write_file` and compiled answer code, each compiled step marked
`[matches the recording]`.

## 3. Run it without the model

| Do this first | Then `hermes freeze run refunds` shows |
|---|---|
| `python3 demo/demo_data.py next-day .` (adds Nov 7) | `read_file × 6 (recording had 5)`, Nov 7 has the most refunds (7), 0 model calls, under a second |
| `python3 demo/demo_data.py break .` (moves `data/`) | `✗ Stopped at s1`, and `refunds.md` is unchanged. Undo with `python3 demo/demo_data.py fix .` |
| `echo "my note" >> refunds.md` | `✗ … changed since freeze last wrote it; not overwriting it`, and your line stays |

## 4. Schedule it

```bash
hermes freeze schedule refunds "0 9 * * *"
```

This creates a no-agent cron job: every run is a replay with 0 model calls, and the agent only
steps in on drift (`--no-fallback` turns that off). `hermes cron list` shows the job, and
`hermes cron remove <job_id>` deletes it.
