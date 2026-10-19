# Repository Guidelines

## Project Structure & Module Organization

This repository currently contains design documents only:

- `AI操作员平台设计文档.docx`: product vision and architecture for an AI operator that observes, controls, and verifies desktop workflows.
- `多Agent协作开发平台_软件设计文档.docx`: architecture for a collaborative development platform with a PM Agent, Code Agents, Web Service, MCP interface, and UI.

No source, test, or asset directories exist yet. Treat architectures and directory layouts described in these documents as proposals, not implemented features. When implementation begins, document the actual module layout and entry points here.

## Build, Test, and Development Commands

No build system, dependency manifest, runnable application, or automated test command is configured. Do not assume commands such as `npm test` or `dotnet build` apply.

Use PowerShell to inspect the current documents:

```powershell
Get-ChildItem -File *.docx
```

Open edited documents in a DOCX-compatible editor to verify rendering. Add exact setup, build, run, and test commands when the first implementation is introduced.

## Coding Style & Naming Conventions

No programming language, formatter, linter, or code indentation convention is established. Introduce and document these alongside the initial source code.

For documentation, preserve the existing Chinese terminology, descriptive filenames, numbered sections, and architecture vocabulary. Use UTF-8 and descriptive headings for Markdown; keep examples concrete. Avoid unrelated reformatting or renaming of design files.

## Testing Guidelines

No testing framework or coverage threshold exists. For document changes, verify headings, tables, diagrams, and cross-references; refresh the table of contents where applicable. Check consistency between both designs and identify unresolved differences explicitly.

For future code changes, include relevant behavioral tests and document their naming convention and execution commands.

## Commit & Pull Request Guidelines

This checkout has no Git metadata, so no historical commit convention can be verified. When version control is introduced, use concise, imperative messages, for example `docs: clarify PM Agent responsibilities`.

Pull requests should describe the purpose, affected documents or modules, validation performed, and unresolved design questions. Link relevant issues when available. For binary DOCX changes, summarize changed sections in the description and include screenshots when layout or diagrams change.
