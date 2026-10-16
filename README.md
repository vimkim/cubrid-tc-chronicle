# CUBRID TC Chronicle

Search testcase failure history across CUBRID commits and CI runs.

Given a failed testcase, Chronicle answers:

- Which tested engine commits show this failure?
- Did this testcase fail before the selected occurrence?
- Where can I inspect the original result and failure details?
- How much relevant history is available, missing, or not yet indexed?

The user selected **CLI first, with a web UI later**. This directory currently contains the project design and research. The commands below describe the proposed interface. No application is implemented yet.

```sh
tcc find cbrd_27461
tcc history sql/_36_guava/cbrd_27461/cases/cbrd_27461.sql
tcc explain --run 38868725373 --suite sql \
  --case sql/_36_guava/cbrd_27461/cases/cbrd_27461.sql
```

Chronicle reports **seen before**, **first observed in the selected window**, or **unknown history**. It does not infer a passing result from an absent failure entry.

The proposed implementation uses Python, SQLite, and a retained local evidence archive. GitHub provides run discovery. CUBRID's internal result store provides detailed testcase outcomes. Queries work offline after synchronization.

## Design documents

- [Project design](docs/design.md): behavior, architecture, data model, collection, and delivery plan.
- [CUBRID evidence](docs/cubrid-evidence.md): verified workflow layout and a real failed-test sample.
- [GitHub evidence](docs/github-evidence.md): discovery, attempts, retention, permissions, and request limits.

Design date: **2026-10-16**. Accepted project name: **CUBRID TC Chronicle**. Repository directory: `cubrid-tc-chronicle`.
