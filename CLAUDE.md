# build-ibge — instructions for Claude

Builders for the IBGE data warehouse (censuses 1991/2000/2010/2022, DTB, POP). See README.md for the layout
and the pipelines, and MEMORY.md for what was done, the decisions taken and the open items.

## Start of a session
- Read MEMORY.md first. Check that what is asked does not redo or contradict something already done or
  decided there (decisions in MEMORY.md and the DECISIONS.md / NOTES.md files are not re-litigated unless the
  user asks).
- At the end, log the session briefly in MEMORY.md (date, what changed, what the user still has to run).

## Data safety
- The warehouse (`<IBGE root>` and the other data folders denied in `.claude/settings.json`) is written only by the user. Claude reads it, and tests
  scripts on outputs pointed at a scratch folder (a copy of the config with output/report paths changed).
- Never delete or overwrite warehouse files; hand the user the exact commands to run.
- After the user runs something, check its log and compare the outputs with the scratch test.

## Code conventions
- Every script takes `--config <json>`; nothing hardcoded (paths, years, lists, thresholds go in the config).
- Every config exists as `<name>.example.json` (data paths under `/path/to/IBGE`) and `<name>.local.json`
  (this machine). Change both together. In-repo paths in configs are relative to the script's folder; scripts
  are run from their own folder.
- One folder per dataset, flat; shared census tools in `censos/`. Do not add packages or nesting unless asked.
- Script header docstring: what it does, the checks it makes, a `CONFIG (JSON, --config)` section listing
  required and optional keys with defaults, and `Usage:`. Match the style of the neighbouring scripts.
- Outputs: write `<file>.part`, rename when complete; refuse to overwrite without `--overwrite`; log to
  `report_folder` (log file + CSV reports); exit non-zero when a check fails.
- Keep the naming conventions of README.md (file names, `<entity>_code` / `<entity>_name`, flags -9999/-1/-2, `NA`).
- The hand-kept catalogs (`IBGE_*_LAYERS_X_X.csv`, `IBGE_*_FIELDS_X_X.csv`) in the dataset folders are the
  source; keep their line endings (LAYERS CRLF, FIELDS LF) and tell the user to copy them into T1.
- New dependencies go into requirements.txt with the tested version.
