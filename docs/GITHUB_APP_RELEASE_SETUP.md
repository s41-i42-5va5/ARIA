# ARIA 1.5.5 release GitHub App setup

This checklist is the exact browser-side registration contract for the ARIA release GitHub App.
It does not authorize creation by itself. The administrator reviews the completed GitHub form
before the final **Create GitHub App** action.

## Registration settings

- GitHub App, not an OAuth App.
- Enable Device Flow: enabled.
- Request user authorization during installation: disabled.
- Callback URL: not required for device flow.
- Webhook Active: disabled; ARIA uses bounded polling through the Windows coordinator task.
- Webhook URL and secret: not required.
- Account permissions: none.
- Repository selection at installation: only selected repositories.
- Installation ownership: choose **Any account** only when the same release App must support
  repositories outside the App owner's account; otherwise restrict it to the App owner.
- User access tokens remain expiring; ARIA supports refresh-token rotation.

## Repository permissions

| GitHub App permission | Registration level | ARIA use |
|---|---|---|
| Administration | Read and write | User token creates a repository/ruleset; coordinator installation token requests only read |
| Checks | Read and write | Publish `ARIA integration` on the exact PR head and verify required checks |
| Contents | Read and write | Git clone/push and coordinator-only Git object/ref updates |
| Issues | Read and write | Developer request queue, coordinator comment and close |
| Metadata | Read-only | Repository identity, collaborators, permissions and ruleset read-back |
| Pull requests | Read-only | Bind open/merged PRs to backlog items and verified authors |
| Commit statuses | Read-only | Verify legacy required status contexts |

No Actions, Workflows, Deployments, Secrets, Members, Organization administration, or account
permission is required by the implemented ARIA 1.5.5 workflow.

The GitHub App registration must grant the superset above. ARIA deliberately requests a smaller
one-repository installation token at runtime: `administration: read`, `checks: write`,
`contents: write`, `issues: write`, `metadata: read`, `pull_requests: read`, and
`statuses: read`.

## Public values and private key

After creation, record these public values:

- App ID / coordinator integration id;
- Client ID used by device flow.

Generate one private key only for the coordinator machine. Import it locally:

```text
aria github-app configure --app-id <app-id> --private-key <downloaded-pem>
aria github-app status --app-id <app-id>
```

ARIA stores the imported key in Windows Credential Manager. The PEM is not committed, included in
the release bundle, placed in project documents, or sent to developers. Delete the downloaded PEM
through the administrator's approved secure-file process only after `status` confirms read-back.

Developers receive no App key. They authorize their own immutable GitHub identity through device
flow and keep their user/refresh tokens in their own Windows Credential Manager.

## Installation sequence

1. Create the App after reviewing the exact form.
2. Generate and import the coordinator private key on the administrator PC.
3. Install the App into only the private acceptance repository.
4. Put the public Client ID and App ID into the final schema 3 manifest.
5. Re-run exact final-bundle install/read-back.
6. Run the four-user private GitHub acceptance scenario.
7. Only then change the bundle from `candidate` to a publishable status.

Official GitHub references:

- https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app
- https://docs.github.com/en/apps/maintaining-github-apps/modifying-a-github-app-registration
- https://docs.github.com/en/rest/repos/rules?apiVersion=2026-03-10
- https://docs.github.com/en/rest/collaborators/collaborators?apiVersion=2026-03-10
- https://docs.github.com/en/rest/repos/repos?apiVersion=2026-03-10
