# ap-pelican-group-provisioner

Scripts that synchronize LDAP users and groups into a Pelican Origin. Packaged as a single
Docker image, configured by a YAML file plus environment variables for secrets.

## Scripts

| Command            | Job kind   | Design                                                       | Purpose |
|--------------------|------------|--------------------------------------------------------------|---------|
| `sync-collections` | `users`    | [design/user-collections.md](design/user-collections.md)     | A personal collection for every member of an LDAP group. |
| `sync-collections` | `projects` | [design/project-collections.md](design/project-collections.md) | A collection per Topology project, granted to a group of its members. |

One command runs every job in `config.yaml`.

## Configuration

Structure goes in a YAML file, secrets go in the environment, and any environment variable
overrides the same setting in the file. See `config.example.yaml` for every setting and
`.env.example` for the secrets. The file is found via `CONFIG_FILE`, then `./config.yaml`,
then `/etc/provisioner/config.yaml`.

The `jobs` list maps LDAP groups to base paths. A `users` job gives each member a collection
at `<base_path>/<username>`. A `projects` job matches members' other group memberships against
the project names in a Topology JSON feed and gives each project with members a collection at
`<base_path>/<project>`, granted to a Pelican group of the project's members.

The provisioner owns each base path exclusively. Anything under it that is not a current
member's collection is deleted, and nothing outside it is ever touched. A run that would
delete more than 5% of a job's collections stops before changing anything; set
`ALLOW_MASS_DELETE=true` for one run when that is intended, for example after changing a
`base_path`.

## Local development

```
uv sync
cp config.example.yaml config.yaml   # edit jobs, origin, ldap
cp .env.example .env                 # fill in secrets
uv run --env-file .env sync-collections
```

Run the tests with `uv run pytest`.

## Container

```
docker build -t ap-pelican-group-provisioner .
docker run --rm --env-file .env \
  -v "$PWD/config.yaml:/etc/provisioner/config.yaml:ro" ap-pelican-group-provisioner
```

In Kubernetes, mount the file from a ConfigMap and inject the secrets from a Secret as
environment variables.

Set `DRY_RUN=true` for a first run against a new Origin. Exit code is 0 on success, 1 when
some users could not be processed, and 2 when a hard error stopped the run.

## Testing a user's access

`scripts/test-collection-access.sh <username> [other-username]` checks that a provisioned
user's collection grant really gives them data access, talking only to the Origin. It runs
a device-code login against the Origin's embedded issuer (you log in as the user in a
browser), prints the scopes on the minted token, then PUTs, GETs and DELETEs an object under
the user's prefix and confirms a PUT under another user's prefix is refused.

```
CURL_INSECURE=1 NAMESPACE=/test BASE_PATH=/test/data scripts/test-collection-access.sh cannon.lock aaron.moate
```

`NAMESPACE` is the Origin's exported namespace (which selects the issuer) and `BASE_PATH` is
the job's base path. Set `ORIGIN_URL` and `DATA_URL` for a non-local Origin. Requires `curl`
and `jq`.
