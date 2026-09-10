# Third-Party Notices

This project's own source under `src/hoh/**` imports exactly one third-party package at runtime. The list below was produced by parsing the AST of every file under `src/hoh/` and excluding both the standard library and the `hoh` package itself (measured 2026-09-08); it is not typed by hand and should be regenerated the same way if dependencies change.

## Third-Party Dependencies

- `pydantic`

## Details

- `pydantic` is declared as a dependency in `pyproject.toml`.
- `pydantic` 2.11.7's license is MIT, verified locally with no network: `importlib.metadata.metadata("pydantic").get("License-Expression")` reports `MIT` on the installed distribution, and that distribution's own `pydantic-2.11.7.dist-info/licenses/LICENSE` file reads "The MIT License (MIT)", "Copyright (c) 2017 to present Pydantic Services Inc. and individual contributors."
- `pydantic` pulls in `pydantic-core` 2.33.2 as its compiled backend; this project's own source does not import `pydantic-core` directly, but it ships as part of the installed `pydantic` distribution, so its license is recorded here too. It is MIT, verified locally with no network the same way: `importlib.metadata.metadata("pydantic-core").get("License")` reports `MIT` on the installed distribution, and that distribution's own `pydantic_core-2.33.2.dist-info/licenses/LICENSE` file reads "The MIT License (MIT)", "Copyright (c) 2022 Samuel Colvin."
- No affiliation with, or endorsement by, the pydantic or pydantic-core maintainers is implied by this project's use of them.
