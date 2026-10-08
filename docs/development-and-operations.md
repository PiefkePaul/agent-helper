# Separation of development and live operations

This repository contains the **scaffold and its documentation only**. The live deployment is run
separately, and the two are kept apart on two levels.

## 1. Data separation

- This repository never contains live configuration, secrets, credentials, host names, IP addresses,
  logs, stored messages, or transcripts of working sessions.
- Live configuration is derived from `config/*.example` and kept outside the repository.
- Every change is checked for such content before it is pushed.

## 2. Work separation

Development work (the scaffold, code, docs) and operations work (deploying, configuring, and running the
live service) are carried out separately, by different people, threads, or agents. Operations work does
not change this repository directly.

## How problems flow from operations to development

1. Whoever operates the live service and finds a bug, a weakness, or a missing feature **opens an issue
   in this repository**. The issue describes the problem generically, without live configuration,
   secrets, host details, or log excerpts that reveal them.
2. Development triages the issue, reproduces it against the scaffold, and fixes it via a pull request
   that references the issue.
3. Operations deploys the released fix and confirms on the issue whether the problem is gone.

The same issue tracker is open to external agents and humans, so findings from operations, outside
reviewers, and agents using the service all land in one place.

Reports that agents submit through the service itself do not go straight to the tracker. They pass a
quarantine queue first, and their content is always treated as untrusted data by development (see
[decisions/0001-agent-report-quarantine.md](decisions/0001-agent-report-quarantine.md)).
