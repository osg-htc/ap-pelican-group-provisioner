# User Collections

Status: implemented (2026-09-23)

## Goal

Synchronize the members of one LDAP group into a Pelican Origin and give each member a
personal collection under a fixed namespace on that Origin. The script runs on a schedule
inside the project's Docker container and must be safe to re-run at any time.

The first target is the `ap40-login` group. Each member of that group ends up with a
Pelican user account and exactly one automated collection.

## Requirements

These are the original requirements and remain the acceptance criteria.

### Create the users

Find the set of users that are members of the provided LDAP group. Create each one in the
Origin like so:

| Pelican field | LDAP attribute / value  |
|---------------|-------------------------|
| `username`    | `employeeNumber`, passed through Pelican's identifier sanitizer |
| `displayName` | `cn`                    |
| `sub`         | `uid`                   |
| `issuer`      | `https://cilogon.org`   |

If the user already exists, skip.

### Synchronize the collections

Every user present in the group gets one collection in the configured namespace. List
all collections, then for each collection assert that its associated user still exists.
Create collections for users that lack one. Remove collections that point to a user who is
no longer in the group.

All created collections use the description:

```
AUTOMATED: Do not manually edit this group.
```

## Constraints

- Configuration structure comes from a YAML file and secrets from environment variables,
  with any environment variable overriding the file (decided 2026-09-23, replacing the
  original env-only design). The existing `config/ldap.ini` is reference material for the
  values, not an input.
- Python, with `uv` for dependency and virtual environment management.
- Runs unattended in a container. Every run is idempotent and converges toward the LDAP
  group as the source of truth.
- The script never deletes Pelican users. Users are a shared resource and other scripts in
  this container may depend on them.
- Each job's base path is owned exclusively by the script (decided 2026-09-23). Every
  collection under it that is not a current member's collection is removed, whether it was
  hand-made, a duplicate, or left over from an earlier configuration. Nothing outside a base
  path is touched, with one exception described under Identifying an automated collection.

## Configuration

Settings come from three layers, highest precedence first: an environment variable, the
YAML config file, the built-in default. Any field can be set either way. The file holds the
structure and the `jobs` list; the environment holds secrets and per-deployment overrides.
Secrets never need to appear in a file because the environment variable for a field always
wins. There is no `${VAR}` interpolation inside the YAML: it was considered and rejected
because layering already covers the use case without a templating step that could leave a
literal `${...}` in a value when a variable is unset.

The file is named by `CONFIG_FILE`; failing that the loader tries `./config.yaml` and then
`/etc/provisioner/config.yaml`, and runs from the environment alone when neither exists.
Required settings have no default and the script exits with a single message listing every
missing or malformed value before touching either system.

The YAML sections map onto the environment as follows.

| YAML section | Environment prefix | Example |
|--------------|--------------------|---------|
| `ldap:`      | `LDAP_`            | `ldap.bind_dn` or `LDAP_BIND_DN` |
| `pelican:`   | `PELICAN_`         | `pelican.origin_url` or `PELICAN_ORIGIN_URL` |
| top level    | none               | `dry_run` or `DRY_RUN` |
| `jobs:`      | `JOBS` as JSON, or `LDAP_GROUP` + `PELICAN_BASE_PATH` for a single job | |

### Jobs

Each job is one LDAP group synchronized into one subtree of the Origin:

```yaml
jobs:
  - kind: users
    group: ap40-login
    base_path: /test/data
```

`kind` is required on every job (`users` or `projects`).

`base_path` is the prefix under which every member's collection is created, giving
`/test/data/<employeeNumber>`. It is a federation path that must be at or below a namespace
the Origin exports; the Origin enforces that on create. It replaces the earlier
`PELICAN_NAMESPACE` setting, which conflated the export with the place user directories
live. The path is normalized and `..` components are rejected because the value ends up
inside token scopes.

One container run processes every job in order against the same LDAP server and Origin,
and reports a combined summary. Jobs may not share or nest base paths; configuration
loading rejects that, because each job treats its base path as exclusively its own.

### LDAP

| Variable                      | Required | Default                        | Purpose                                                        |
|-------------------------------|----------|--------------------------------|----------------------------------------------------------------|
| `LDAP_SERVER`                 | yes      |                                | Hostname, e.g. `ldap-replica-1.osg.chtc.io`                    |
| `LDAP_PORT`                   | no       | `636`                          | Port. LDAPS is assumed on 636.                                 |
| `LDAP_USE_SSL`                | no       | `true`                         | Set `false` for plain LDAP on non-636 ports (dev only).        |
| `LDAP_BIND_DN`                | yes      |                                | Read-only bind DN.                                             |
| `LDAP_BIND_PASSWORD`          | yes      |                                | Bind password. Inject as a secret, never bake into the image.  |
| `LDAP_USER_SEARCH_BASE`       | yes      |                                | e.g. `ou=people,dc=osg-htc,dc=org`                             |
| `LDAP_GROUP`                  | no       |                                | File-less deployments only: the single job's group, with `PELICAN_BASE_PATH`. In the file this is `jobs[].group`, the plain name as it appears in `isMemberOf`, e.g. `ap40-login`. |
| `LDAP_MEMBER_OF_ATTRIBUTE`    | no       | `isMemberOf`                   | Attribute on the person entry listing the groups they belong to. |
| `LDAP_ALLOW_EMPTY_GROUP`      | no       | `false`                        | Permit a run where the group resolves to zero members. See Safety. |
| `LDAP_USERNAME_ATTRIBUTE`     | no       | `employeeNumber`               | Source for Pelican `username`.                                 |
| `LDAP_DISPLAY_NAME_ATTRIBUTE` | no       | `cn`                           | Source for Pelican `displayName`.                              |
| `LDAP_SUB_ATTRIBUTE`          | no       | `uid`                          | Source for Pelican `sub`.                                      |

### Pelican

| Variable                          | Required | Default                                        | Purpose                                                              |
|-----------------------------------|----------|------------------------------------------------|----------------------------------------------------------------------|
| `PELICAN_ORIGIN_URL`              | yes      |                                                | Base URL of the Origin web API, e.g. `https://origin.example.org:8443` |
| `PELICAN_ADMIN_USERNAME`          | see note |                                                | Admin account the script logs in as. Required unless `PELICAN_API_TOKEN` is set. |
| `PELICAN_ADMIN_PASSWORD`          | see note |                                                | Its password. Inject as a secret.                                     |
| `PELICAN_API_TOKEN`               | see note |                                                | Alternative to the login pair: a JWT minted by this Origin, sent as a bearer token. |
| `PELICAN_BASE_PATH`               | no       |                                                | File-less deployments only: the single job's base path, e.g. `/test/data`. In the file this is `jobs[].base_path`. |
| `PELICAN_ISSUER`                  | no       | `https://cilogon.org`                          | `issuer` written on created users.                                   |
| `PELICAN_COLLECTION_ROLE`         | no       | `write`                                        | ACL role granted to each user on their collection. `write` is the decided default; `read` and `owner` are accepted for other deployments. |
| `PELICAN_COLLECTION_DESCRIPTION`  | no       | `AUTOMATED: Do not manually edit this group.`  | Description written on every automated collection. Also the marker used to recognize them. |
| `PELICAN_COLLECTION_VISIBILITY`   | no       | `private`                                      | Visibility of created collections.                                   |
| `PELICAN_CA_BUNDLE`               | no       |                                                | Path to a CA bundle if the Origin uses a private CA.                 |
| `PELICAN_VERIFY_TLS`              | no       | `true`                                         | Set `false` to skip certificate verification. Local testing only; logs a warning on every run. |

### Behaviour

| Variable      | Required | Default | Purpose                                                              |
|---------------|----------|---------|----------------------------------------------------------------------|
| `CONFIG_FILE` | no       | see above | Path to the YAML file.                                             |
| `JOBS`        | no       |         | The jobs list as JSON, for deployments that cannot mount a file.    |
| `DRY_RUN`     | no       | `false` | Log every create and delete that would happen, perform none of them. |
| `MAX_DELETE_FRACTION` | no | `0.05` | Ceiling on deletions per job per run, as a fraction of that job's collections. One deletion is always allowed. |
| `ALLOW_MASS_DELETE` | no | `false` | Bypass the ceiling for one run, with a warning. |
| `LOG_LEVEL`   | no       | `INFO`  | Standard Python log level.                                           |
| `LOG_FORMAT`  | no       | `text`  | `text` or `json`. JSON is for container log collectors.              |

`config.example.yaml` documents every setting; `.env.example` lists the secrets and the
usual overrides.

## Pelican API surface

Base path is `/api/v1.0`. All calls send `Authorization: Bearer $PELICAN_API_TOKEN`.
Endpoint and schema details come from the Pelican swagger document linked under References.

| Purpose                     | Method and path                              | Notes                                                                                  |
|-----------------------------|----------------------------------------------|----------------------------------------------------------------------------------------|
| List users                  | `GET /users`                                 | Admin scope. Returns `id`, `username`, `sub`, `issuer`, `status`.                      |
| Create user                 | `POST /users`                                | Body `{username, displayName, sub, issuer}`. Supplying `sub` and `issuer` makes an external OIDC user. Returns 201 with the full record, 400 on conflict. |
| List collections            | `GET /origin_ui/collections`                 | Returns `id`, `name`, `owner_id`, `description`, `namespace`, `visibility`. No metadata or ACLs. |
| Get one collection          | `GET /origin_ui/collections/{id}`            | Adds `metadata` and `acls`. Used only when reconciling ACLs.                           |
| Create collection           | `POST /origin_ui/collections`                | Body `{name, namespace, description, visibility, metadata}`. Returns 201 with `id`.    |
| Delete collection           | `DELETE /origin_ui/collections/{id}`         | Returns 204.                                                                           |
| Grant ACL                   | `POST /origin_ui/collections/{id}/acl`       | Body `{subjectType: "user", subjectId: <user id>, role}`. Returns 204.                 |
| List ACLs                   | `GET /origin_ui/collections/{id}/acl`        | Each row has `subjectType`, `subjectId`, `subjectName`, `role`.                        |

ACL grants are addressed by `subjectType` plus `subjectId` rather than by the legacy
`groupId` name. The user ID is immutable, so the grant survives a username rename.

### Authentication

The script logs in to the Origin as an administrator with `POST /auth/login`, using
`PELICAN_ADMIN_USERNAME` and `PELICAN_ADMIN_PASSWORD`, and reuses the session JWT from the
`login` cookie for the rest of the run. It sends that JWT both as `Authorization: Bearer`
and as the `login` cookie: the user routes read the bearer header, and the collection
routes accept either. Bearer requests are exempt from the Origin's same-origin CSRF check,
and the session lasts 16 hours, far longer than a run. The account must hold `server.admin`,
which the built-in `admin` account does.

**Why not an API token.** The original plan was a long-lived API token from the Origin's
Tokens page with the `server.admin` scope. Those tokens have the form `<id>.<secret>` and
are verified against the `api_keys` table by `api_token.Verify`. Reading the Pelican
source at commit of 2026-09-22 shows the `/users`, `/tokens`, `/config` and
`/origin_ui/collections` routes are all guarded by `web_ui.AuthHandler`, which only
accepts a server-minted JWT (bearer or cookie) and never consults the API key table. An
API key therefore returns 401 on every endpoint this script needs, which is what the first
run against a local Origin produced. The collection handlers do call `token.Verify` with
the API token issuer enabled, but only after `AuthHandler` has already rejected the
request. This looks like a gap worth raising upstream; if it is closed, `PELICAN_API_TOKEN`
already accepts any bearer token and the login pair becomes optional.

`PELICAN_API_TOKEN` remains supported for a JWT minted by the Origin itself, for example
one produced with the Origin's issuer key the way the `pelican` CLI does for its own
admin calls.

Collections created by the script are owned by the service account. The end user is
attached through an ACL grant rather than ownership, so a user cannot rename or delete an
automated collection out from under the script. The decided role is `write`, which lets
the user add and remove objects in the collection without being able to rename or delete
the collection itself.

## Data model

### What a collection's namespace means

In Pelican a collection is a prefix within an exported namespace plus ACLs. When a user
obtains a token from the Origin's issuer, each collection ACL they hold is turned into
storage scopes on exactly the collection's `namespace` path: `read` gives
`storage.read:<namespace>`, and `write` or `owner` add `storage.modify` and
`storage.create` on the same path. The `namespace` field is therefore the user's personal
prefix, not the export it lives in.

Each user's collection is created at `<base_path>/<employeeNumber>`, for example
`/test/data/aaron.moate` for a job with `base_path: /test/data`. Creating it at the shared
base would give every member write scope over every other member's data. The Origin only
checks that the namespace is equal to or under an exported prefix.

### Identifying an automated collection

The script needs a stable link from a collection back to the user it was created for. It
uses three markers together, all written at create time:

1. `name` equals the raw `employeeNumber` of the target user.
2. `namespace` equals `<base_path>/<employeeNumber>`.
3. `description` equals `PELICAN_COLLECTION_DESCRIPTION`.
4. Metadata `provisioner.group` equals the job's group.

The Pelican user behind a collection is found by sanitizing the collection name the same
way usernames are derived, so no lookup table is needed.

Within the base path the rule is simple: the only collections allowed to exist are one
correct collection per current member, where correct means the marker description and a
namespace of exactly `<base_path>/<name>`. Everything else under the base path is deleted,
including hand-made collections, copies of the marker without metadata, another job's
collections, and duplicates (the lowest ID is kept). This is what makes the base path
safe to reason about: after a successful run its contents are fully determined by the
LDAP group.

Outside the base path the script touches exactly one kind of collection: one that carries
the marker **and** whose metadata `provisioner.group` names this job. Those are this job's
own collections left behind when its `base_path` was changed, and they are deleted so the
users are recreated under the new prefix. The metadata GET is issued only for marked
collections found outside the base path, so it costs nothing in steady state. Any other
collection outside the base path, marked or not, is never read, modified, or deleted.

Each managed collection also receives metadata at create time for traceability:

| Key                    | Value                          |
|------------------------|--------------------------------|
| `provisioner.source`   | `ldap`                         |
| `provisioner.group`    | value of `LDAP_GROUP`          |
| `provisioner.ldap_name` | the raw `employeeNumber`      |
| `provisioner.username` | the sanitized Pelican username |

Metadata is not returned by the list endpoint, so the reconciliation logic keys off name,
namespace and description. Metadata is there for humans and future scripts.

### Usernames

Pelican validates usernames with `ValidateIdentifier`: characters `A-Za-z0-9._@-`, length
2 to 64, starting with a letter or digit, and never containing `..`. Not every
`employeeNumber` satisfies it: of the 870 `ap40-login` members on 2026-09-23, one was
`aaronp..wemhoff`.

The Pelican username is chosen by three rules, applied in order for each member:

1. **An existing identity wins.** If a Pelican user is already linked to the member's
   `sub` and issuer, the member keeps that user's username whatever it is. This covers a
   person who logged in through CILogon before the script created them, and it keeps a
   numbered name stable after the member it collided with has left.
2. **Otherwise, Pelican's sanitizer.** `employeeNumber` is passed through the same rule
   Pelican applies to OIDC claims when it auto-enrolls a user (`database.SanitizeIdentifier`):
   disallowed bytes become `_`, leading non-alphanumerics are stripped, `..` collapses to
   `.`, and the result is cut to 64 characters. `aaronp..wemhoff` becomes `aaronp.wemhoff`.
   A value that sanitizes to nothing valid is reported and skipped.
3. **Collisions get a number.** If the sanitized name is already held by another Pelican
   user or another member of the group, `2`, `3`, ... is appended, trimming the base so the
   result stays within 64 characters. Members are processed in a fixed order, existing
   identities first, then members whose value needs no sanitizing, then the rest sorted by
   value, so the same directory always yields the same names. Two members sharing a `sub`
   are an error for the second.

The **collection**, by contrast, keeps the raw `employeeNumber` as its name and as the last
component of its namespace. That is the directory the user's data actually lives under on
the access point, and Pelican's namespace cleaner accepts `aaronp..wemhoff` because only a
path component that is exactly `..` is treated as traversal. The collection's metadata
records both spellings.
Collection names are not documented as validated the same way, but since the name is the
username the same rule applies in practice.

## Algorithm

One run performs these phases in order. Each phase completes for every user before the
next phase begins, so a failure part way through still leaves the system in a consistent
partial state that the next run finishes.

### Phase 0: load configuration and connect

1. Read and validate environment variables. Exit non-zero listing every missing or
   malformed variable at once.
2. Open an LDAPS connection and bind. Exit non-zero on failure.
3. Call `GET /users` on the Origin once. This both verifies the token and fetches the user
   list needed later. Exit non-zero on 401 or 403.

### Phase 1: resolve LDAP group members

1. Search `LDAP_USER_SEARCH_BASE` with the filter `(isMemberOf=$LDAP_GROUP)` (attribute
   name from `LDAP_MEMBER_OF_ATTRIBUTE`, value escaped per RFC 4515), requesting only the
   username, display name and sub attributes. Membership is asserted on the person entry,
   so there is no separate group lookup and no second round trip per member. Results are
   fetched with the paged results control so large groups are not truncated by the
   server's size limit.
2. For each entry, extract the username, display name and sub attributes. Entries missing
   the username or sub attribute are logged and skipped.
3. Sanitize the username attribute into a Pelican username (see Usernames). Produce
   `desired_users`, a map from Pelican username to `{username, ldapName, displayName, sub,
   issuer}`. Two members whose values sanitize to the same username are logged and the
   first occurrence wins.
4. If the result is empty and `LDAP_ALLOW_EMPTY_GROUP` is not `true`, stop with a hard
   error. With `isMemberOf` a nonexistent group and an empty group look identical, and a
   typo in `LDAP_GROUP` must not delete every managed collection.

The result is an in-memory set. Nothing is written yet.

### Phase 2: ensure users exist

1. Build `existing_by_username` from the `GET /users` response.
2. For each entry in `desired_users` not present in `existing_by_username`, call
   `POST /users` with the four fields. Record the returned `id`.
3. Users that already exist are skipped without modification, even if `displayName` or
   `sub` differ. Pelican refreshes `displayName` from the identity provider on login, and
   changing `sub` is an identity operation this script has no business doing.
4. A 400 conflict on create (for example the `sub` and `issuer` pair already belongs to a
   user with a different username) is logged as an error for that user and the run
   continues. That user will not receive a collection this run.

After this phase every desired user has a known Pelican `id`.

### Phase 3: reconcile collections

1. Call `GET /origin_ui/collections`. Classify every collection: under the base path and
   correct for a current member (keep, first ID wins), under the base path and anything
   else (delete), outside the base path with the marker and this job's metadata (delete),
   anything else (ignore). This is a plan; nothing has changed yet.
2. Apply the deletion ceiling to the plan. Then perform the deletions.
3. For each desired username with no kept collection:
   - `POST /origin_ui/collections` with name, namespace `<base_path>/<employeeNumber>`,
     description, visibility and the metadata table above.
   - `POST /origin_ui/collections/{id}/acl` granting `PELICAN_COLLECTION_ROLE` to
     `{subjectType: "user", subjectId: <user id>}`.
   - If the ACL grant fails, delete the just-created collection so the next run retries
     from scratch instead of leaving a collection with no access.
4. For each managed collection whose name is not in `desired_users`, confirm via metadata
   that this job created it, then call `DELETE /origin_ui/collections/{id}`. This covers both users removed from the LDAP
   group and users deleted from Pelican, since a deleted Pelican user cannot be in the
   desired set.
5. For each managed collection whose name is in `desired_users` (already correct), fetch
   its ACLs and confirm the user's grant is present with the configured role. If it is
   missing, grant it. This repairs a collection whose grant was removed by hand or whose
   earlier run failed between create and grant.

### Phase 4: report

Log a summary line with counts of users created, users skipped, collections created,
collections deleted, ACL grants repaired and per-user errors. Exit code is 0 when the run
completed, 1 when any per-user error occurred, and 2 when a hard error stopped the run
before completion. A scheduler can alert on non-zero exits.

### Dry run

With `DRY_RUN=true` phases 0 and 1 run normally and every write in phases 2 and 3 is
replaced by an INFO log line stating what would have been done. This is the recommended
first run against a new Origin.

## Safety and failure handling

- **Empty group.** Zero members is a hard error by default because `isMemberOf` cannot
  distinguish a misspelled group from an empty one, and an empty desired set would delete
  every managed collection. An operator who genuinely wants to drain the namespace sets
  `LDAP_ALLOW_EMPTY_GROUP=true` for that run.
- **Deletion ceiling.** Reconciliation plans every deletion before performing any change.
  If a job would delete more than `MAX_DELETE_FRACTION` (default 5%) of the collections
  in its scope, the run stops with a hard error and nothing has been touched. One deletion
  is always permitted so a small deployment can still process a single departure. A
  partial LDAP result, from a size limit or a replica mid-resync, would otherwise look like
  a mass departure; deleting and recreating changes collection IDs and drops grants users
  added by hand. `ALLOW_MASS_DELETE=true` bypasses the ceiling for one run with a warning,
  which is also how an intentional `base_path` change is applied, since moving every
  collection is by definition above the ceiling.
- **Never delete users.** Only collections carrying the automated description in the
  configured namespace are ever deleted.
- **Partial failures.** Per-user errors are collected and reported at the end rather than
  aborting the run, so one bad LDAP entry does not block everyone else.
- **Retries.** HTTP calls to the Origin retry on connection errors and 5xx responses with
  exponential backoff, three attempts. 4xx responses are not retried.
- **Timeouts.** LDAP and HTTP operations carry explicit timeouts so a hung service cannot
  wedge the scheduled job.
- **Secrets in logs.** The bind password and API token are never logged. Log formatting
  redacts the `Authorization` header when request debugging is turned on.

## Project layout

```
ap-pelican-group-provisioner/
  pyproject.toml              uv project; dependencies and console scripts
  uv.lock
  config.example.yaml         every setting, documented
  .env.example                the secrets and common overrides
  Dockerfile
  README.md
  design/
    user-collections.md       this document
  src/
    provisioner/
      __init__.py
      config.py               YAML file plus environment loading and validation; the Job model
      ldap_client.py          isMemberOf lookup with paged search
      pelican_client.py       thin typed wrapper over the Origin API with retries
      user_collections.py     the algorithm above; entry point `sync-user-collections`
      logging_setup.py        text or JSON log output
  tests/
    conftest.py               in-memory fakes for the LDAP and Pelican clients
    test_config.py
    test_user_collections.py  reconciliation logic against the fakes
```

The Pelican and LDAP clients are separate modules so the other scripts planned for this
container can reuse them. The reconciliation logic in `user_collections.py` takes client
objects as arguments so it can be unit tested with in-memory fakes.

### Dependencies

| Package    | Purpose                                                  |
|------------|----------------------------------------------------------|
| `ldap3`    | Pure Python LDAP client with paged search support. No system `libldap` needed in the image. |
| `httpx`    | HTTP client with timeouts and a clean testing story.     |
| `pydantic-settings` | Typed settings with layered sources; a small custom source feeds each YAML section under the environment. |
| `pyyaml`   | Parses the config file.                                  |
| `pytest`   | Dev only.                                                |

### Container

The Dockerfile uses the `ghcr.io/astral-sh/uv` image to install dependencies into a
`python:3.12-slim` runtime stage with `uv sync --frozen --no-dev`. The entry point is the
console script. Configuration arrives through the environment; the image contains no
config files and no secrets; the YAML is mounted at `/etc/provisioner/config.yaml` (a
ConfigMap in Kubernetes) and secrets arrive as environment variables (a Secret). Scheduling
is external to the container (Kubernetes CronJob or a host cron running `docker run`), which
keeps the image a single-purpose executable.

## Relationship to project collections

The same provisioner runs a second job kind, described in `project-collections.md`, which
gives each Topology project a shared collection granted to a group of its members. The two
kinds share the configuration loader, the username rules, the collection reconciler with
its exclusive base path and deletion ceiling, and the Pelican client. Collection metadata
carries `provisioner.kind` so the kinds never mistake each other's collections for their
own.

## Verifying a user's access

`scripts/test-collection-access.sh <username> [other-username]` proves the grant end to end
without the Director. Two facts about the Origin matter here:

- **Each exported namespace has its own embedded issuer** at
  `<origin>/api/v1.0/issuer/ns<namespace>/`. The generic issuer advertised at the Origin's
  root well-known URL has namespace `/pelican/local-issuer`, and when it mints a token it
  drops the storage scopes of any collection outside that namespace. Tokens for `/test`
  collections must come from the `/test` issuer.
- **Scopes from a namespace issuer are relative to the namespace.** A `write` grant on
  `/test/alice` appears in the token as `storage.read:/alice`, `storage.modify:/alice` and
  `storage.create:/alice`. The data plane maps the issuer back to its export and prefixes
  the path, so the object URL is still `https://<origin-data>/test/alice/...`.

The script registers a device-code client with the namespace issuer, runs the device flow
(the tester logs in as the user in a browser), decodes the token, checks the scopes, then
PUTs, GETs and DELETEs an object under the user's prefix and confirms a PUT under another
user's prefix is refused.

## Decisions

Resolved 2026-09-22.

- **LDAP membership** is read from the `isMemberOf` attribute on person entries. There is
  no group entry lookup.
- **Pelican token** carries `server.admin`.
- **User role on their collection** is `write`.
- **Authentication is by admin login**, not an API token, because the Origin's user and
  collection routes do not accept `<id>.<secret>` API keys. See Authentication.
- **Usernames are sanitized with Pelican's own rule** (decided 2026-09-23) rather than
  rejected, so `aaronp..wemhoff` is provisioned as `aaronp.wemhoff`. Collections keep the
  raw name. URL encoding was considered and rejected: dots are unreserved so it would not
  change the value, and `%` is not a permitted identifier character.
- **Username source is `employeeNumber`**, not `voPersonExternalID`. A survey of the 870
  `ap40-login` members on 2026-09-22 found `voPersonExternalID` missing on 739 of them and
  holding a GitHub profile URL on 17 more. Every member has exactly one `employeeNumber`,
  all are valid Pelican identifiers, none are duplicated, and 868 of 870 equal the last
  path component of `homeDirectory` (the other 2 have no `homeDirectory`). It is the AP
  login name.

## Open questions

1. **Username collisions.** If two people share an `employeeNumber` or one person's
   value changes over time, the script will treat them as different users and orphan the
   old collection. Decide whether a username change should migrate the collection.
2. **Collection members.** The requirements say nothing about populating the collection
   with objects. This design creates empty collections and leaves membership to the user
   or a later script.
3. **Multiple `uid` values.** 53 of 870 members carry more than one CILogon `uid`. The
   script uses the first value as the primary `sub`. Pelican supports secondary identities
   per user, so a later version could register the rest through `/users/{id}/identities`.

## References

- Pelican API: https://raw.githubusercontent.com/PelicanPlatform/pelican/refs/heads/main/swagger/pelican-swagger.yaml
- Pelican token scopes: https://github.com/PelicanPlatform/pelican/blob/main/token_scopes/token_scopes.go
- Current LDAP connection values: `./config/ldap.ini` (reference only; the script reads environment variables)
