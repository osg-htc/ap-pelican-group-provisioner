# ap-pelican-group-provisioner

Built to provision LDAP groups to AP40 and AP41.

Scripts that synchronize LDAP users and groups into a Pelican Origin. Packaged as a single
Docker image, configured by a YAML file plus environment variables for secrets.

## Scripts

| Command            | Job kind   | Design                                                       | Purpose |
|--------------------|------------|--------------------------------------------------------------|---------|
| `sync-collections` | `users`    | [design/user-collections.md](design/user-collections.md)     | A personal collection for every member of an LDAP group. |
| `sync-collections` | `projects` | [design/project-collections.md](design/project-collections.md) | A collection per Topology project, granted to a group of its members. |

One command runs every job in `config.yaml`.

## Operations contract (TL;DR)

**What it does.** Each run makes the Origin match LDAP and Topology, then exits. Running it
again with nothing changed makes no changes, so it is safe to run on a schedule.

- `users` job: every member of the LDAP group gets a collection at
  `<base_path>/<employeeNumber>` that only their own Pelican user can write to.
- `projects` job: every Topology project with at least one member gets a collection at
  `<base_path>/<project>`, writable by a Pelican group named after the project whose members
  are exactly the project's LDAP members.
- People are matched to Pelican users by CILogon identity (`sub`)

**What it owns, and will change without asking.**

- Everything under each job's `base_path`. Any collection there that it did not create for a
  current member or project is deleted, including ones made by hand.
- The membership of each project group. Members added by hand are removed on the next run.
- Project groups anyone else controls. A project group owned by another account, or with
  another user or group as its administrator, is deleted and recreated under the
  provisioner's account.

**What it never does.**

- Delete Pelican users.
- Touch collections outside the base paths. The one exception is its own collections left at
  an old `base_path`, identified by their metadata.
- Remove a grant someone added directly to a collection. To give a person extra access, grant
  them on the collection, not through the project group.
- Remove group members Pelican added from a CILogon login.
- Create a collection from a name containing `/`, or one that is `.` or `..`. Those members or
  projects are reported and skipped; dots inside a name are fine.

**When it stops before changing anything** (exit code 2):

- More than 5% of a job's collections would be deleted, or more than 5% of its project groups
  replaced. One is always allowed. Set `ALLOW_MASS_DELETE=true` for one run if intended, for
  example after changing a `base_path` or the account the provisioner runs as.
- An LDAP group comes back empty (`LDAP_ALLOW_EMPTY_GROUP=true` to allow it).
- LDAP, the Topology feed or the Origin cannot be read, or its own account cannot be identified.
- A hard error in one job also skips the jobs listed after it.

**Exit codes.** `0` = everything reconciled. `1` = finished, but some people or projects could
not be processed; the log says which, and the next run retries them. `2` = stopped; see above,
or an unexpected error. Alert on `2`, and on `1` that persists across runs.

**What operations must provide.**

- One run at a time, for example a CronJob with `concurrencyPolicy: Forbid`. Overlapping runs
  can create duplicates.
- At most one `projects` job per Origin (config validation enforces this).
- A stable admin account (`PELICAN_API_TOKEN`, or admin username and password) that has
  accepted the Origin's AUP. Switching accounts makes every project group look foreign; see
  `ALLOW_MASS_DELETE` above.
- `DRY_RUN=true` for the first run against a new Origin.

**Known limits.**

- People bulk-imported without a CILogon ID have a placeholder `sub`
  (`dummy-text-for-provisioning-…`) and cannot log in to the account made for them. When
  COManage records their real `sub`, the next run moves their access to an account with the
  real identity, named e.g. `alice2`, because the placeholder account keeps `alice`.

## Configuration

Structure goes in a YAML file, secrets go in the environment, and any environment variable
overrides the same setting in the file. See `config.example.yaml` for every setting and
`.env.example` for the secrets. The file is found via `CONFIG_FILE`, then `./config.yaml`,
then `/etc/provisioner/config.yaml`.

The `jobs` list maps LDAP groups to base paths, and every job names its `kind`. A `users` job
gives each member a collection at `<base_path>/<username>`. A `projects` job matches members'
other group memberships against the project names in a Topology JSON feed and gives each
project with members a collection at `<base_path>/<project>`, granted to a Pelican group of the
project's members. At most one `projects` job is allowed per Origin.

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

### Integration tests against a real Origin

```
scripts/test-origin.sh                 # start Origin, run tests/integration, remove Origin
scripts/test-origin.sh -k crash -x     # extra arguments go to pytest
KEEP_ORIGIN=1 scripts/test-origin.sh   # leave it running; later `uv run pytest` includes these tests
```

This starts `hub.osg-htc.org/pelican_platform/origin:v26.0` as a standalone Origin (no
Director or Registry) on https://localhost:18444 using `tests/integration/compose.yaml`, mints an
admin token inside the container with `pelican-server token create`, and runs the provisioner
against it with LDAP and the Topology feed faked. The tests check access by following each
collection's ACLs on the Origin through group membership to users, as people join, leave, move
between projects and come back. The crash-recovery tests stop a run before each of its writes in
turn, check that the partial state never gives anyone access they would not have before or after
the change, and check that the next run converges. Without a running Origin these tests are
skipped. Requires Docker with Compose.

CI (`.github/workflows/test.yaml`) runs the unit and integration tests together on every push
to `main` and every pull request, with `PELICAN_TEST_REQUIRE_ORIGIN=1` so an Origin that fails to
start fails the build instead of skipping the integration tests.

To point the tests at a different Origin, set `PELICAN_TEST_ORIGIN_URL` and either
`PELICAN_TEST_CONTAINER` (where to run `pelican-server token create`) or `PELICAN_TEST_TOKEN`.

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


## Token Workaround

```shell
pelican-server token create --issuer https://localhost:8444 --subject admin --raw-scope "server.admin web_ui.access" --lifetime 864000 pelican://localhost:8444/test        
```