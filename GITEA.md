# Gitea – verbindliche Hinweise für dieses Repo

Stand 18.11.2026. Gilt für Menschen und KI-Agenten (Claude, Claude Code, Codex, ChatGPT). Geht älteren Angaben in README, CLAUDE.md oder AGENTS.md vor (z. B. „GitHub ist Hauptquelle“, „ghcr.io“).

## Auf einen Blick

| Thema | Stand |
|---|---|
| Hauptquelle | Gitea `https://git.scholz-cloud.de/Scholz/agent-helper` (öffentlich, Org `Scholz`) |
| Git-Adresse | `gitea:Scholz/agent-helper.git` (SSH-Alias `gitea`) |
| GitHub | `github.com/PiefkePaul/agent-helper` (öffentlich) ist eine automatische Kopie (git-sync, beide Richtungen) |
| Standard-Branch | `main`, geschützt: nur per PR, kein Force-Push |
| Issues, PRs, Releases, Pakete | in Gitea |
| CI | Gitea Actions, Workflows bleiben in `.github/workflows/` |
| Runner | Tests `ubuntu-latest`; Image-Job `[self-hosted, nas, agent-helper]` |
| Images | Gitea-Registry, Pull `git.scholz-cloud.de/piefkepaul/agent-helper` |

## Git und Zugang
- `origin` zeigt auf Gitea: `git remote set-url origin gitea:Scholz/agent-helper.git`. Die GitHub-Adresse darf als Remote `github` bleiben, gepusht wird dorthin von Geräten aus nicht.
- Zugangstoken kommen vom Token-Broker nach `~/.config/gitea/gitea.env`. Nie Tokens ins Repo schreiben.
- Gitea hat keine offene Registrierung. Beiträge von außen kommen über GitHub. PRs aus fremden Forks übernimmt git-sync nicht automatisch, Paul übernimmt sie von Hand.
- KI-Agenten nutzen für Gitea den MCP-Connector `Gitea-mcp` oder die Gitea-API, nicht `gh`.

## Zusammenspiel mit GitHub (git-sync)
- git-sync vergleicht Branches und Tags alle 60 s und überträgt nur die geänderte Seite (Dauer 1–2 Minuten).
- Geräte pushen nach Gitea. Cloud-Sessions (sehen nur GitHub) dürfen Branches nach GitHub pushen und dort PRs öffnen; der PR wird in Gitea automatisch angelegt. Gemergt wird in Gitea.
- Auf GitHub keine Issues und keine Releases anlegen.
- Konflikt (beide Seiten haben denselben Branch verschieden geändert): git-sync überschreibt nichts, legt `sync-konflikt/github/<branch>` und ein Gitea-Issue „Sync-Konflikt: …“ an. Nicht selbst per Force-Push auflösen, Paul Bescheid geben. Abgelehnte Pushes erscheinen als Issue „Sync-Fehler: …“.
- Tags werden nur angelegt, nie verschoben oder gelöscht.

## Merge und Deploy
- Änderungen immer auf einem Branch mit PR in Gitea. Gemergt wird von Paul oder mit seinem ausdrücklichen Go.
- Merges deployen nie automatisch. Deploy per Hand auf eine feste `sha-…`-Version, Rollback = vorige `sha-…`.

## CI (Gitea Actions)
- Jobs ohne Docker: `runs-on: ubuntu-latest` (Gitea-Runner `gitea-nas-1`, bewusst ohne Docker-Zugang).
- Docker- und Image-Jobs: `runs-on: [self-hosted, nas, agent-helper]` auf einem CI-Manager-Platz. Das Repo ist dort eingetragen.
- Öffentliche Repos bekommen CI-Manager-Plätze nur für vertrauenswürdige Läufe: Push auf den Hauptbranch oder einen Tag, PRs aus Branches dieses Repos, `workflow_dispatch`, `schedule`, `release`, jeweils von PiefkePaul ausgelöst. Fork-PRs und Läufe Fremder warten dauerhaft.
- Auf GitHub laufen Jobs nur bei Push nach `main`. Nur-GitHub-Jobs: `if: github.server_url == 'https://github.com' && github.ref == 'refs/heads/main'`. Nur-Gitea-Jobs: `if: github.server_url != 'https://github.com'`.
- `actions/upload-artifact@v4`/`download-artifact@v4` gehen in Gitea nicht, dort `@v3` nehmen. Kein Actions-Cache `type=gha`.

## Container-Images
- Push nur im LAN an `gitea:3000/<besitzer>/<image>` (über die öffentliche Adresse scheitern Uploads über 100 MB). Builder mit `driver-opts: network=host` und `buildkitd-config-inline` (`http = true`, `insecure = true` für `gitea:3000`).
- Pull: `git.scholz-cloud.de/<besitzer>/<image>:sha-<kurz>`.
- Login: Benutzer `piefkepaul`, Passwort `secrets.REGISTRY_TOKEN` (Org-Secret). Es gibt kein `GITEA_REGISTRY_TOKEN`: Gitea verbietet Secret-Namen mit `GITEA_`/`GITHUB_`.
- Tags nur von `main`: `latest` und `sha-<7 Zeichen>`. PRs und andere Branches bauen nur.
- Nicht nach `ghcr.io` pushen. Docker Hub ist als Zweitkopie erlaubt (`DOCKERHUB_*`).
- Vorlage: NAS-Wiki `infrastruktur/github-gitea-umzug/vorlage-docker-workflow`.

## Secrets und Variablen
- Org-Secrets `Scholz`: `REGISTRY_TOKEN`, `DOCKERHUB_TOKEN`, `DOCKERHUB_USERNAME`, `DOCKER_USERNAME`, `DOCKER_PASSWORD`.
- Fehlende Werte nie erfinden: Fundort im NAS-Wiki `infrastruktur/schluessel-inventar`, dann Paul fragen.

## Stand in diesem Repo
- `ci.yml`: `test` auf `ubuntu-latest`, Image-Job auf `[self-hosted, nas, agent-helper]`, Image `piefkepaul/agent-helper` nur von `main` (Umstellung vom 17.11., CI grün). Keine weiteren Secrets nötig.
- Das Image liegt unter dem Besitzer `piefkepaul`, nicht `scholz` wie in der Vorlage für öffentliche Repos. Bewusst so lassen oder beim nächsten Umbau angleichen (dann Deploy mit anpassen).
- Deploy-Umstellung auf das Gitea-Image ist geplant (NAS-Wiki `services/agent-helper/konfiguration`).

## Quellen (NAS-Wiki, intern)
- `infrastruktur/github-gitea-umzug/plan`, `.../ci-umstellung`, `.../vorlage-docker-workflow`
- `services/git-sync/status`
- `projekte/agent-helper/known-errors`
- `infrastruktur/schluessel-inventar`
