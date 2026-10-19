# Parser fixtures

`provenance.json` records each specimen's origin and SHA-256 digests. `bundle.json` maps parser evidence names to local filenames. Use `uv run cubrid-tcc inspect PATH/bundle.json` to parse a fixture without changing the local index.

SQL and medium fixtures contain recorded public verdict bytes. The successful and failed samples cover both suites. Medium failure provenance came from an existing retained collection, with its plan checked against the live store.

Shell fixtures are sanitized structural extracts from real CTP JUnit output. Paths and provenance are synthetic. Failure bodies were removed. Counters describe the retained subset. The original private files remain outside this repository.

The replay tests construct partial reruns, incomplete shards, unsupported verdicts, and inconsistent metadata explicitly. They do not present synthetic records as live observations.
