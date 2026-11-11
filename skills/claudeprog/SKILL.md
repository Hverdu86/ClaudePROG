---
name: claudeprog
description: Opens a pop-out desktop window with a countdown, progress bar and live hardware load (GPU, GPU memory, processor, memory) while a long command runs, and shows "Project complete" with a close button when it ends. Use it whenever you are about to run a command in the background that could take more than 10 minutes, such as model training, data or document conversion, large builds, long test suites, renders, downloads or batch jobs. Also use it when the user asks for a timer, a countdown, a progress window, or a way to see that a job is still running and how hard their GPU or computer is working.
---

# ClaudePROG

A long job running in the background is invisible to the person waiting for it. This skill
runs the job through `scripts/claudeprog.py` (in this skill's folder), which starts the job,
opens a small window on the person's desktop, and reports back when the job ends.

The window shows a countdown to the estimated finish, a progress bar, and live load for the
GPU, GPU memory, processor and memory. It stays on top of other windows and can be dragged
anywhere. When the job ends it says "Project complete" and offers a "Close window" button.
If the job fails it says "Job stopped early" instead, with the exit code.

## When to use it

- Before starting any background command that could run longer than 10 minutes.
- When the person asks for a timer, countdown or progress window for something already running.

Short commands need nothing: the window only opens once a job has run for 10 minutes, or
immediately when your estimate is above 10 minutes, so wrapping a job that turns out to be
quick costs nothing.

## Starting a job

Run this as a background command, so that you are told when the job ends:

```
python "<this skill's folder>/scripts/claudeprog.py" --title "Model training" --minutes 90 --log train.log -- python train.py --epochs 30
```

- Everything after `--` is the command, exactly as you would have run it.
- `--title` is a few plain words the person will recognise.
- `--minutes` is your honest estimate for the whole job. Leave it out if you have no basis
  for one; the window then counts up instead of down.
- `--log` is where the job's output goes. Read this file to check on the job.
- Use the Python the project already uses. Only the standard library is needed, with Tk
  (included in the python.org installers; on Linux it is the `python3-tk` package).

The command prints the job's process id, starts the job detached, and returns when the job
ends, printing the exit code, the duration and the last lines of output. The job keeps
running if this command, the window, or the session is closed.

If the job prints progress such as `[25/300]`, the window switches from your estimate to
the job's real pace. For another format pass `--pattern` with a regular expression that has
two groups, done and total, for example `--pattern "step (\d+) of (\d+)"`.

## Watching a job that is already running

```
python "<this skill's folder>/scripts/claudeprog.py" --title "Video export" --minutes 45 --pid 12345 --log export.log
```

`--minutes` then means the time left from now, unless you also pass `--started` with the
time the job began, in seconds since 1970.

## Other options

- `--show-now` opens the window immediately, whatever the estimate.
- `--show-after 300` changes the 10-minute wait, in seconds.
- `--theme dark` or `--theme light`; by default it follows the system.
- `--cwd <folder>` runs the command in that folder.
- `--demo` shows the window with a one-minute pretend job, to let the person see it.

## What to tell the person

- That the window is open, or when it will open, and that the countdown is an estimate.
- That closing the window does not stop the job.
- How to stop the job if they want to, using the process id the command printed.

When the command returns, check the job's actual results before saying it worked. Exit
code 0 means the program ended normally, not that its output is right.

## Limits

- Tested on Windows 11. macOS and Linux are written for but not yet tested.
- GPU readings come from `nvidia-smi`, so they need an NVIDIA card; otherwise they show "n/a".
  On macOS, processor and memory readings need the optional `psutil` package.
- It watches a process on this computer. For work running on another machine, watch the
  local command that waits for it.
- If the command prints that the window did not stay open, Python on this computer probably
  has no Tk. Say so, and give the person the path of the error file it names.
