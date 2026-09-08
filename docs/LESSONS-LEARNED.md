# Notes for whoever edits this next

Behaviours in ArcGIS Pro's Python toolbox framework that are not obvious from the
documentation, and design decisions in this repository that look arbitrary until you know
why. Most of these cost real debugging time.

## The dialog

**`Parameter.description` is real, but Pylance does not know it.** Setting `.description`
on a parameter is what populates the help panel in the geoprocessing dialog, and it works.
ArcGIS's own type stubs do not declare the attribute, so a type checker reports
`Cannot assign to attribute "description"` on every one of them — around a hundred false
errors on this toolbox. Same for `.filter.list`, which the stubs type as possibly `None`.
Do not "fix" these. Validate the toolbox by loading it (see
[DEVELOPMENT.md](DEVELOPMENT.md)) rather than by trusting the type checker.

**`Parameter.displayName` cannot be assigned after construction.** It looks like an
ordinary writable attribute and does not complain at construction time, but assigning to
it from inside `updateParameters()` raises `AttributeError`. An exception thrown there
leaves the whole dialog in a broken state, showing warning markers on unrelated
parameters. The dynamically writable properties are `.value`, `.enabled`, `.category`,
`.filter.list`, and the `setErrorMessage()` / `setWarningMessage()` methods.

**A value table column's pick-list is fixed at construction.** Assigning
`param.filters[i].list` later, from `updateParameters()`, updates neither the choices nor
the widget type of an already-rendered dialog. Confirmed twice on live runs. A plain (non
value-table) `GPString` parameter's `.filter.list` *is* dynamically updatable — the
limitation is specific to value tables. If a value table column's choices can only be
known at runtime, leave it as free text and communicate the values another way.

**A `Field` column in a value table turns its dependent parameter into a table view.**
There is a way to get a real picker inside a value table — declare the column as `Field`
and point `parameterDependencies` at the table parameter, which ArcGIS then resolves
itself, sidestepping the limitation above. The cost is severe and not obvious: ArcGIS
registers the dependent input as a table view, so its `valueAsText` returns a view name
rather than a path, and everything that read that input as a file stops working. One
optional parameter took out the tool's primary input and every other pick-list on the
dialog. The parameter objects looked perfectly correct when inspected; only a real dialog
showed the damage.

**`updateParameters()` and `updateMessages()` must never raise, and cannot log.**
`arcpy.AddMessage()` writes to the run-time messages pane, which does not exist while the
dialog is open — anything logged there is silently discarded. To surface a problem found
during validation, capture it onto the tool instance and report it with
`setWarningMessage()` on the relevant parameter. This repository does that for telemetry
header reads and profile loads.

**Look parameters up by name, never by index.** Use
`params_by_name = {p.name: p for p in parameters}`. Index-based access breaks silently
when a parameter is inserted or reordered — the wrong parameter is modified and nothing
raises. Prefer `.get()` inside the validation callbacks: ArcGIS Pro caches a tool's
parameter list for the life of a loaded toolbox, so a newly added parameter is absent
from a stale session and a direct subscript would raise a `KeyError` that disables the
whole tool.

**Validate in `updateMessages()`, not in `execute()`.** Checking that a folder exists or a
file is readable during validation puts the error in the dialog immediately. The same
check inside `execute()` only fails after the user has waited for processing to start.

## Coordinate systems

**An unset spatial reference is truthy.** `arcpy.SpatialReference()` with nothing set is
still a real Python object, so `spatial_reference or fallback` never selects the fallback.
Check `.factoryCode` or `.name` instead. This produced a confusing failure where the same
value was accepted by one geoprocessing tool and rejected by the next.

**Prefer `.value` over `.valueAsText` for a `GPCoordinateSystem` parameter.** The dialog
supplies a real `SpatialReference` object through `.value`, which is the common case and
always correct. `.valueAsText` can return WKT2 containing a time-dependent datum clause
that `arcpy.SpatialReference(text)` cannot parse, which silently falls back to a default
and produces wrong coordinates. A coercion helper should try `.factoryCode` first and only
then fall back to text.

**Raw WKT must go through `loadFromString()`.** Passing WKT as the positional argument to
`arcpy.SpatialReference(...)` routes it through a name/file lookup, which never accepts
WKT text.

## Geoprocessing tool calls

**Capture `arcpy.GetMessages()` before any cleanup runs.** A cleanup step in a `finally`
block that itself calls a geoprocessing tool — even something as small as `Delete` —
resets the message stack. By the time the caller's `except` handler asks for the messages,
the real failure has been replaced by the cleanup call's own empty output. Catch the error
next to the call that raised it, read the messages there, and re-raise carrying the text.

**Never route messages by searching for "ERROR" anywhere in the string.** A warning that
quotes an underlying error contains the word too. Any call to `arcpy.AddError()` marks the
whole run as failed, so a misrouted warning makes a successful run look broken. Check the
message's own leading prefix, after stripping indentation.

**A wrong string for a registry-matched parameter can crash without a message.** Some
geoprocessing parameters are free text matched against an internal registry rather than a
validated enumeration. Supplying an unregistered value can produce a bare, detail-free
`ERROR 999999` instead of "invalid value", sending you off investigating the data instead
of the parameter. Verify such strings against a working example or the exact dialog label.

**Verify keyword values against the documentation, not against the keyword's name.** A
plain-English reading of a geoprocessing keyword can be the opposite of what it does for
your case.

**Convert Video Metadata field-matches only the fields Esri documents.** Its reference
page lists thirteen; anything else is dropped, under any spelling. Far distance is not
among them, and neither is it in the multiplexer's own
`FMV_Multiplexer_Field_Mapping_Template.csv` (`C:\Program Files\ArcGIS\Pro\Resources\MotionImagery`),
which is the authoritative list of 76 accepted headings — the only distance tags there are
21 `Slant Range` and 57 `Ground Range`. So a per-frame far distance cannot be carried to
the multiplexer as such, whatever the column is called.

**Do not inject values into the converted file to fill a gap.** Writing a value into a
column the multiplexer does not recognise turns a harmless `WARNING 003950: Empty metadata
value` into `WARNING 002651: Unable to parse the input metadata file`. An empty column it
ignores; a populated column it cannot map, it complains about. Check a heading against the
template before deciding a blank is a bug.

## Data

**A world file's existence does not mean an image is georeferenced.** Frames from video
extraction typically carry a placeholder world file — pixel size 1, origin near the
origin — describing pixel space, not ground coordinates. It is well formed and passes
every "does a world file exist" check, while placing every image in the same small square
at the coordinate origin. Read the numbers before trusting them. This is why the tools
compute each frame's real ground extent from the camera model rather than relying on the
world file.

**Use an explicit `is not None` check for numeric parameters.** `value or default`
discards a legitimate `0`. A camera pitch or roll of exactly zero is a meaningful,
common value — a level camera — not a missing one.

**Field names in real tables vary.** Column names differ between software versions and
between vessels, so fields are resolved once per table against an ordered list of
candidate names, case-insensitively, rather than being hard-coded. Add new spellings to
the candidate lists rather than renaming anyone's data.

**Decide how to read a table by what it is, not by what it is called.** A CSV added to a
map becomes a table view whose name still ends in `.csv`. An extension test therefore
routes it to the file-reading branch, and `open()` fails on something that was never a
path — which surfaces as empty field pick-lists and a "table does not exist" message
rather than as anything to do with reading. Resolve the value through
`arcpy.Describe().catalogPath` first, then check whether the result really is a file.
Reading a genuine CSV directly is still worth the branch: `arcpy.ListFields()` returns
sanitised names — `Depth M` comes back as `Depth_M` — which then match nothing in the raw
rows.

## Architecture in this repository

**Core modules load by explicit file path, not through `sys.path`.** Each `FCT_*_core.py`
is loaded with `importlib.util.spec_from_file_location` from the toolbox's own directory.
Mutating `sys.path` is fragile across machines and interacts badly with how ArcGIS Pro
isolates modules. The practical consequence: **the `.pyt` and every core module must stay
in the same folder.**

**Core modules are re-read from disk before every run.** ArcGIS Pro keeps a loaded
toolbox's modules alive across runs in one session, so editing a core module on disk is
not by itself enough to change the next run's behaviour. Every `execute()` re-loads them
first and logs each file's modification time — check those lines before concluding a fix
did not work.

**The set-level iFDO document is built directly from values in memory.** It is not
assembled by writing per-image sidecars and reading them back. An earlier design did
that, and a later change to where sidecars are stored silently broke it, producing no
output and no error. Building from memory removes the dependency on file layout
entirely.

**Metadata generation runs in two passes when exporting.** The first pass embeds metadata
into the source images; the second generates sidecars in the export folder. This keeps
the input folder clean while still producing a self-contained deliverable.

**An imported template contributes values, not a file.** Template values are merged into
the generated metadata; the template file itself is deliberately excluded from the export
so the output folder contains only results.
