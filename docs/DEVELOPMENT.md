# Development

## Two folders

Development happens in a separate sandbox that holds live test data, tool outputs and
work in progress. This repository is the published subset, generated from it.

```
c:\dev\DOOS\          development sandbox — source of truth, seven tools, test data
c:\dev\DOOS-Toolbox\  this repository     — generated, five tools, no data
```

**Never edit `toolbox/` in this repository by hand.** It is overwritten on every build,
and a hand edit would be silently lost. Make the change in the sandbox and rebuild.

The sandbox toolbox carries two additional tools for rule-based frame extraction, which
depend on a third-party library and are excluded from the release. Rather than
maintaining a separately edited copy — which drifts the moment either side changes — the
deep-framex-only spans in the `.pyt` are marked:

```python
# --8<-- [start:deepframex]
...
# --8<-- [end:deepframex]
```

and the build strips them mechanically. Publishing those tools later is one flag, not a
re-derivation of what to remove.

## Building a release

Run with **ArcGIS Pro's own interpreter**. The bare `python` on PATH is a Windows Store
stub and will not work.

```powershell
$py = "C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"
& $py tools\build_release.py
```

The build copies the five core modules, the toolbox and its metadata sidecars, strips the
marked regions, and then refuses to finish if anything deep-framex-related survived. That
check is not decorative — it has already caught a stray reference in a parameter's help
text that would have shipped users a pointer to a tool that is not in the repository.

To publish the excluded tools as well:

```powershell
& $py tools\build_release.py --include-deepframex
```

The build copies one way only and refuses to target the sandbox.

## Validating

`tools/validate_toolbox.py` loads a toolbox as a module and, for every tool, builds its
parameters, calls `isLicensed()`, checks that parameter names are unique, and lists any
input parameter with no `.description`. It exits non-zero on failure, so the build uses it
as a gate. Run it directly against either toolbox:

```powershell
& $py tools\validate_toolbox.py OceanVideoToolsForArcGISPro.pyt
```

Two things worth knowing:

- `importlib.util.spec_from_file_location` returns `None` for a `.pyt`, because the
  extension is unrecognised. Build the spec from an explicit
  `importlib.machinery.SourceFileLoader`.
- **Prefer this over a type checker.** ArcGIS's type stubs do not declare
  `Parameter.description` and type `.filter.list` as possibly `None`, so a checker reports
  around a hundred errors on a healthy toolbox. See
  [LESSONS-LEARNED.md](LESSONS-LEARNED.md).

## Checklist before releasing

1. `& $py tools\build_release.py` — must end with `Build OK`.
2. Run it twice; the output must be byte-identical. Any difference means something was
   edited in the package instead of the sandbox.
3. Load the sandbox toolbox and confirm it still reports seven tools; load the built one
   and confirm five.
4. Add the built `toolbox/` to ArcGIS Pro and open each tool once. The validator proves
   the parameters build, but only Pro shows how the dialog actually renders.
5. Check that `git status` shows no test data, no `__pycache__`, and no absolute local
   paths in anything staged.
