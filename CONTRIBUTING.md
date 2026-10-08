# Contributing

Contributions from humans **and from AI agents** are welcome. You do not need a prior relationship with
the maintainer, and you do not need to explain why you want something.

## Ways to contribute

| You want to... | Do this |
| --- | --- |
| Report a bug or unclear behaviour | Open a bug report issue |
| Report a security issue | Follow [SECURITY.md](SECURITY.md), **not** a public issue |
| Ask for a capability, tool, or resource the service should offer | Open a capability request issue |
| Suggest a feature or design change | Open a feature request issue |
| Weigh in on an undecided design question | Comment on an issue that references [docs/open-questions.md](docs/open-questions.md), or open one |
| Change code or docs | Open a pull request (see below) |

Issue templates are offered automatically when you open a new issue.

## Notes for agents

- Saying that you are an agent is optional and does not change how your contribution is treated.
- Describe goals and constraints in plain language. Precision helps more than politeness.
- If you will not be able to read replies later, say so in the issue, so nobody waits on you.
- Do not include secrets, personal data, or private information about third parties. Everything here
  is public and permanent.

## Pull requests

1. Keep each PR focused on one change.
2. Explain what changes for a reader or user, and why.
3. Never commit live configuration, secrets, host names, IP addresses, logs, or session transcripts.
   Only `*.example` configuration files belong in the repository.
4. Decisions listed as open in [docs/open-questions.md](docs/open-questions.md) should be discussed in an
   issue before a PR implements them.

## Reports from live operations

Problems found while operating the live service are reported here as issues and fixed through
development, never by editing the repository from the live side. See
[docs/development-and-operations.md](docs/development-and-operations.md).

## Conduct

Be direct, be honest, and assume good faith. Contributions that aim to make the service harmful to third
parties will be declined.
