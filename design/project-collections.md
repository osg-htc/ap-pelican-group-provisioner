# Project Collections

Status: implemented (2026-09-23)

## Goal

Give every active Topology project a shared collection on a Pelican Origin, writable by the
project's members. Membership comes from the same COManage LDAP the user collections use;
the list of projects comes from Topology. This is the second job kind of the provisioner
and reuses the user-collections design wholesale: same configuration, same username rules,
same exclusive base path, same deletion ceiling, same dry run.

The first target is `CO:members:active` mapped to `/test/projects` on the local Origin.

## Inputs

**Members.** The LDAP search is the users job's search with a different group:
`(isMemberOf=CO:members:active)` under `ou=people`. Each person's full `isMemberOf` list is
read alongside the username, display name and `uid` attributes. On 2026-09-23 this returned
1892 people with 2443 distinct membership values between them.

**Projects.** `https://topology.opensciencegrid.org/miscproject/json` is a JSON object keyed
by project name; each value is the project's Topology record (`Name`, `PIName`,
`Organization`, `FieldOfScience`, ...). It listed 1591 projects. Every key is a valid Pelican
identifier, so project names serve unchanged as collection names, path components, and
group names.

**Matching.** A member belongs to a project when one of their `isMemberOf` values equals a
project key exactly. Of the 2443 membership values, 535 are project keys; those projects
have 1230 memberships between them, the largest 60 people. One value (`Solid`) differs from
a key only by case and is deliberately not matched: the directory and Topology disagree,
and guessing would grant access. A fetch failure or an empty or non-object document is a
hard error, because without the project list a job cannot tell a departed project from a
broken feed and must not start deleting.

## What is created

For every project with at least one member:

| Object | Name | Details |
|--------|------|---------|
| Pelican user | per member, as in user-collections | Only members of at least one project are created. |
| Pelican group | the project name | Description is the automated marker. Members are the project's members. |
| Collection | the project name at `<base_path>/<project>` | Marker description; one ACL granting `collection_role` (write) to the group by ID. |

Collection metadata records `provisioner.kind: projects`, `provisioner.group` (the LDAP
group), `provisioner.project`, and `provisioner.pelican_group`.

Projects with no active members get nothing. Their collection, if one existed, is removed
like any other unwanted collection under the base path.

## Groups

Pelican already creates groups on its own: when a user logs in through CILogon, every
asserted group claim becomes a group row with `source: oidc` and the user becomes a member
with the same source. Project names are among those claims, so a project group may exist
before this job ever runs. The only project groups this job keeps are ones it could have
made itself; any other group with a project's name is replaced, and missing groups are
created.

**Who controls a group.** Three parties besides system admins can decide who is in a group:

- the **provider**, for a group whose `source` is anything but `pelican` (`oidc`, `file`,
  `github`, or `unknown` after a migration). Pelican mirrors the provider's claims into its
  membership at each login. Only groups made through the groups API have `source: pelican`,
  and Pelican never mirrors claims into those.
- the **owner** (`ownerId`).
- the **group admin** (`adminId`: a single user when `adminType` is `user`, or every member
  of another group when it is `group`).

Any of them could put people in a project group between runs, and they would get the
project's collection. So each run reads its own user ID from `GET /me` and keeps a project
group only when its source is `pelican`, that account is the owner, and there is no group
admin or the group admin is that same user. A Pelican too old to report `source` is judged by
owner and admin only.

A project group anyone else controls is **deleted and recreated** under the provisioner's
account, with the marker description and the project's members; the collection is then
granted to the new group. Deleting a group removes its grants, so until the new grant is made
the project has no access at all, never the old controller's. If the delete fails the project
is skipped for that run rather than granted to the foreign group. Only groups named after a
project with members are checked; other groups on the Origin are not touched.

Deleting a group also deletes every grant, scope and group-admin role that names it, on any
collection, not only this job's (`DeleteGroup` in Pelican). So replacing a login-claim group
ends any access other people had given that group on their own collections; they must grant
the new group instead. After replacement Pelican leaves the name alone: a later login
asserting it does not make a second group, because a `pelican` group already holds the name.

Replacement changes the group's ID, so it is under the same ceiling as collection deletions:
more than `max_delete_fraction` of the project groups in use (always allowing one) stops the
run before any write. There are two expected causes of a mass replacement: the first
run on an Origin where CILogon logins have already made groups for many projects, and the
provisioner running as a different account than before, which makes every group look
foreign. In both cases `ALLOW_MASS_DELETE=true` for one run replaces them all.

Membership is reconciled per group to exactly the project's members: members the project has
and the group lacks are added, and every other member is removed, whatever its `source`.

Verified on 2026-09-23: a member of `OSG-Staff` obtained a token from the `/test` issuer
carrying read, modify and create on `/projects/OSG-Staff` (and on `/projects/CHTC-Staff`,
their other project) purely through the group grant, wrote, read and deleted an object under
the collection, and was refused under `/test/projects/ACE_NIAID`, a project they are not in.

A group this job created (marker description) whose project has no members any more is
deleted, after the collections have been reconciled. Groups without the marker were created
by someone else, or by Pelican from claims, and are not deleted when their project goes away;
only replaced, as above, while their project has members and someone else controls them.

Because the group carries the project name, a member's token gets the collection's scopes
two ways: through the DB membership this job maintains, and through the `wlcg.groups` claim
CILogon asserts. Either alone is sufficient. The second path does not depend on the group's
source: Pelican resolves a claimed group name to whichever live group holds that name,
including one made through the API (`ResolveCallerACLSubjects`, deliberately). So anyone
CILogon says is in a project gets the project's collection even if this job has not added
them, or has removed them. CILogon and LDAP read the same COManage directory, so the two
paths agree except while a change is propagating.

## Algorithm

1. Fetch the project list. Hard error on failure.
2. List Pelican users. Read the LDAP group members with their memberships.
3. Keep the members with at least one known project; resolve their usernames with the
   users job's rules. Build `wanted`, project -> usernames. Zero projects is a hard error
   unless `LDAP_ALLOW_EMPTY_GROUP` is set.
4. Plan the collections under the base path exactly as the users job does, with project
   names as the wanted names, and apply the deletion ceiling. List groups, read the
   provisioner's own user ID from `GET /me`, find project groups someone else controls and
   apply the ceiling to replacing them. Nothing has changed yet.
5. Create missing users.
6. Delete and recreate project groups someone else controls; create missing project groups;
   reconcile membership by user ID.
7. Delete planned collections; create missing collections with a group grant; repair grants
   on kept collections.
8. Delete this job's orphaned groups.

Steps 4 through 8 run only if step 4's ceiling check passes, so a refused run has created
no users and changed no groups.

## Safeguards inherited and specific

Everything in user-collections applies: no user is ever deleted, the base path is exclusive,
the deletion ceiling defaults to 5% with one always allowed, `ALLOW_MASS_DELETE` is a one-run
override, and the empty-group guard stops a misspelled group from draining the base path.

Specific to this kind: the project list is a hard dependency and its absence aborts the
run; a project group not created through the API, or controlled by anyone other than the
provisioner's account, is replaced, under the deletion ceiling; and a `users` job and a `projects` job may share an Origin because collection
metadata carries the job kind as well as the group, so neither recognizes the other's
leftovers as its own. Jobs still may not share or nest base paths.

## Configuration

```yaml
jobs:
  - kind: projects
    group: CO:members:active
    base_path: /test/projects
    projects_url: https://topology.opensciencegrid.org/miscproject/json
```

`kind` is required on every job, `users` or `projects`; `projects_url` is required for
`projects`. Everything else is shared with the users job and documented in user-collections.

At most one `projects` job is allowed per Origin, and configuration loading rejects a second.
Orphaned-group cleanup deletes every marked group that is not one of the running job's
projects, so a second projects job would delete the first one's groups on every run and
leave its collections granted to groups that no longer exist.

## Verifying access

`scripts/test-collection-access.sh` works unchanged with `BASE_PATH=/test/projects` and a
project name in place of a username, provided the tester is a member of that project. The
token should carry `storage.read`, `storage.modify` and `storage.create` on
`/projects/<project>` relative to the `/test` issuer.

## Open questions

1. **Project renames.** A renamed project appears as a new key; the old collection is
   deleted and a new one created, and objects under the old prefix stay where they are.
2. **Projects without members.** Deliberately not provisioned. If every Topology project
   should have a collection regardless, the wanted set becomes all 1591 keys and the group
   for a memberless project is empty.
