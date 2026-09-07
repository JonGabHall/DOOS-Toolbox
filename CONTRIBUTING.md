# Contributing

Contributions, bug reports and suggestions are welcome.

## Reporting a problem

Open an issue and include, where you can:

- what you were trying to do and which tool you ran
- the full text from the geoprocessing **Messages** pane (it usually contains the real
  cause)
- your ArcGIS Pro version and licence level, and whether the Image Analyst extension is
  available
- what your input data looks like — the column names of the table, or a few of the image
  file names. Real values are not needed and should not be posted if they are sensitive

Before opening an issue, check **Troubleshooting** and **Known issues** in the
[README](README.md).

## Making a change

1. Read [docs/LESSONS-LEARNED.md](docs/LESSONS-LEARNED.md) first. Several apparently
   reasonable changes to a Python toolbox do not work in ArcGIS Pro, and that file records
   the ones already discovered the hard way.
2. Read [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for how to validate a change.
3. Keep tool classes thin. All real logic belongs in the matching `FCT_*_core.py` module,
   so it can be read and tested without a dialog.
4. Every new input parameter needs a `.description`. It is what users see in the help
   panel, and the validation script fails without it.
5. Do not add a third-party dependency without discussion. The toolbox deliberately runs
   on a default ArcGIS Pro installation; the one optional package (PyAV) gates a single
   feature and is imported lazily so its absence never breaks anything else.
6. Do not rename a parameter's `name` unless it is necessary — that breaks saved models
   and scripts. Changing a `displayName` is safe.

## Style

- Match the surrounding code. Fields are resolved against candidate name lists rather
  than hard-coded, values are checked with `is not None` rather than truthiness, and
  failures in optional steps warn and continue rather than aborting a run.
- Comment *why*, not *what*. The existing comments record constraints that are not
  visible in the code; keep that habit and skip the rest.

## Acknowledgment

This repository contains code generated or assisted by GitHub Copilot. Contributions
made with AI assistance are welcome on the same terms as any other — the requirements
above apply regardless of how a change was written, and you are responsible for
validating anything you submit against real data in ArcGIS Pro.
