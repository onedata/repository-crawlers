---
audience: maintainer
source_modules:
  - apps/registrar/src/registrar/register/command.py
  - apps/registrar/src/registrar/register/planner.py
  - apps/registrar/src/registrar/register/lookups.py
  - apps/registrar/src/registrar/register/registration.py
  - apps/registrar/src/registrar/config.py
  - apps/registrar/src/registrar/api/onepanel.py
  - apps/registrar/src/registrar/api/onezone.py
  - apps/registrar/src/registrar/api/oneprovider.py
source_commits:
  public-data-crawlers: 7ce5a5e
---

# Registrar Design Overview

<sub>source: `apps/registrar/src/registrar/register/command.py#run`</sub>

The registrar takes a file of crawled dataset metadata and turns it into
registered Onedata resources — files, shares, and optionally public data
records. You point it at a JSON or JSONL file produced by a crawler, it
figures out which Onedata space and storage to use (or creates them), asks
you to confirm, and then registers every dataset in a single batch run.

The `register` command is the only command with significant logic; the
companion `list-spaces` and `list-storages` commands are simple query
wrappers for operator convenience. The entry point is
`apps/registrar/src/registrar/register/command.py#run` — a linear
sequencer that reads top-to-bottom.

## Design Principles

<sub>source: `apps/registrar/src/registrar/register/command.py#_apply_target_plan` · `apps/registrar/src/registrar/register/registration.py#_process_one`</sub>

The `register` command splits its work into three distinct phases — plan,
apply, register — so the operator always sees what will happen before
anything changes. The planner resolves the target space and storage without
creating resources, the confirmation gate lets the operator inspect and
abort, and only then does the applier materialize missing infrastructure.
This design means you can run `registrar register datasets.jsonl` against
a production cluster and review the resolved plan before a single API call
mutates state.

Three more properties shape the design:

- **Per-dataset error isolation.** If one dataset fails to register (bad
  file URL, share conflict, missing metadata), the loop records the failure
  and moves on. The operator gets a full summary at the end rather than a
  partial run that stopped at the first error.

- **Idempotent re-runs.** Every step checks before creating: files are
  looked up by path before registration, shares are matched by name +
  description, handles are checked for existing `handleId`. A run
  interrupted halfway can be re-run with the same input — already-completed
  work is skipped automatically.

- **No rollback, by design.** If the space is created but support fails,
  the operator re-runs the command — the planner will find the existing
  space by name and pick up where it left off. This is simpler and more
  reliable than partial undo logic.

## Space Resolution

<sub>source: `apps/registrar/src/registrar/register/planner.py#_resolve_space` · `apps/registrar/src/registrar/register/lookups.py#find_spaces_by_name`</sub>

The planner resolves *which space* should hold the datasets. It follows
a cascade: explicit ID → name lookup → inference from file URLs, and
either produces a single unambiguous result or fails with a clear error.
It never silently picks one candidate over another.

```mermaid
flowchart TD
    START([space resolution]) --> A{space.id<br/>provided?}

    A -- yes --> B[lookup_space_by_id<br/>OnepanelClient]
    B -- found --> E1["✅ _PlannedSpace<br/>(id=existing, current_storage_id=id)"]
    B -- not found --> ERR1([❌ TargetResolutionError])

    A -- no --> C{space.name<br/>provided?}

    C -- yes --> D[find_spaces_by_name<br/>OnepanelClient, exact match]
    D -- 0 matches --> E2["✅ _PlannedSpace<br/>(id=None, current_storage_id=None)"]
    D -- 1 match --> E3["✅ _PlannedSpace<br/>(id=existing, current_storage_id=id)"]
    D -- 2+ matches --> ERR2([⚠️ AmbiguityError<br/>with candidate IDs])

    C -- no --> F["infer_domain<br/>(first_file_url)"]
    F -- domain found --> G[use domain as name]
    G --> D
    F -- no usable URL --> ERR3([❌ TargetResolutionError])

    classDef error fill:#E63946,stroke:#9D0208,color:#fff
    classDef ambiguity fill:#FFD700,stroke:#F08C00,color:#000
    classDef success fill:#95D5B2,stroke:#2D6A4F,color:#000
    classDef op fill:#4ECDC4,stroke:#0B7285,color:#000

    class E1,E2,E3 success
    class ERR1,ERR3 error
    class ERR2 ambiguity
    class B,D,F,G op
```

The config layer enforces mutual exclusion between `space.id` and
`space.name`, so only one path can be active. When a name is inferred
from file URLs, the plan records `space_inferred=True` so the confirmation
display can highlight the guess before the operator commits.

## Storage Resolution

<sub>source: `apps/registrar/src/registrar/register/planner.py#_resolve_storage`</sub>

Storage resolution is more complex than space resolution because it must
satisfy two additional constraints:

- **Compatibility** — a storage must be HTTP readonly imported (the only
  type that supports file registration from external URLs). Incompatible
  types (POSIX, HTTP non-readonly) are rejected with a clear error.

- **Single-support invariant** — a space on a given provider can be
  supported by only one storage. The planner refuses to plan a switch
  rather than risk data split across storages or an API error at apply
  time.

```mermaid
flowchart TD
    START([resolve_storage]) --> Q_ID{storage.id<br/>provided?}

    Q_ID -- yes --> LOOKUP_ID[lookup_storage_by_id]
    LOOKUP_ID -- not found --> ERR1([❌ TargetResolutionError])
    LOOKUP_ID -- found --> COMPAT_ID{is_storage_compatible?<br/>HTTP readonly imported}
    COMPAT_ID -- no --> ERR2([❌ TargetResolutionError])
    COMPAT_ID -- yes --> SINGLE_ID{storage == space's<br/>current storage?}
    SINGLE_ID -- "yes, or space has<br/>no current storage" --> USE_ID([✅ use this storage])
    SINGLE_ID -- "no — different storage" --> ERR3([❌ TargetResolutionError])

    Q_ID -- no --> Q_NAME{storage.name<br/>provided?}
    Q_NAME -- yes --> FIND[find_storages_by_name<br/>exact match]
    FIND -- "2+ matches" --> ERR4([⚠️ AmbiguityError])
    FIND -- "1 match" --> COMPAT_NAME{is_storage_compatible?}
    COMPAT_NAME -- no --> ERR5([❌ TargetResolutionError])
    COMPAT_NAME -- yes --> SINGLE_NAME{storage == space's<br/>current storage?}
    SINGLE_NAME -- pass --> USE_NAME([✅ use this storage])
    SINGLE_NAME -- fail --> ERR6([❌ TargetResolutionError])

    FIND -- "0 matches" --> Q_CURRENT_NAME{space has<br/>current storage?}
    Q_CURRENT_NAME -- yes --> ERR7([❌ TargetResolutionError<br/>cannot add second support])
    Q_CURRENT_NAME -- no --> ENDPOINT[Infer storage endpoint<br/>from file URL]
    ENDPOINT -- no endpoint --> ERR8([❌ TargetResolutionError])
    ENDPOINT -- got endpoint --> CREATE([✅ plan: create new storage<br/>id = None])

    Q_NAME -- no --> Q_CURRENT_AUTO{space has<br/>current_storage_id?}
    Q_CURRENT_AUTO -- yes --> LOOKUP_AUTO[lookup current storage]
    LOOKUP_AUTO --> COMPAT_AUTO{is_storage_compatible?}
    COMPAT_AUTO -- yes --> REUSE([✅ reuse current storage])
    COMPAT_AUTO -- no --> ERR9([❌ TargetResolutionError<br/>cannot add second support])
    Q_CURRENT_AUTO -- no --> USE_SPACE_NAME[use space.name<br/>as storage name]
    USE_SPACE_NAME --> ENDPOINT

    classDef error fill:#E63946,stroke:#9D0208,color:#fff
    classDef ambiguity fill:#FFD700,stroke:#F08C00,color:#000
    classDef success fill:#95D5B2,stroke:#2D6A4F,color:#000
    classDef op fill:#4ECDC4,stroke:#0B7285,color:#000

    class ERR1,ERR2,ERR3,ERR5,ERR6,ERR7,ERR8,ERR9 error
    class ERR4 ambiguity
    class USE_ID,USE_NAME,REUSE,CREATE success
    class LOOKUP_ID,FIND,LOOKUP_AUTO,ENDPOINT,USE_SPACE_NAME op
```

When neither ID nor name is provided and the space already has a compatible
storage, the planner reuses it. When the space is new, the planner uses
the space name as the storage name — keeping them aligned by convention.

## Public Data Records

<sub>source: `apps/registrar/src/registrar/register/registration.py#_ensure_public_record` · `apps/registrar/src/registrar/api/onezone.py#OnezoneClient.register_handle`</sub>

After files and shares are in place, the registrar can optionally mint a
public identifier for each dataset's share. Both identifier types go
through `register_handle`; the behavior branches on two config values:
`record_identifier_type` (controls `requestPublicHandle`) and
`identifier_policy` (controls `publicHandleToReuse`).

- **`onedata-url`** (default) — registers the handle with
  `requestPublicHandle=false`. No public handle is minted by the service.
- **`pid`** — registers the handle with `requestPublicHandle=true`,
  asking the handle service (e.g. DOI or ePIC) to mint a public handle.

Both types require `metadata_xml` on the dataset and `handle_service_id`
in config.

```mermaid
flowchart TD
    START([public_data_records.enabled?])
    START -- NO --> SKIP([skip record phase])
    START -- YES --> XML{dataset.metadata_xml<br/>present?}

    XML -- NO --> XML_ERR([❌ RecordRequirementError])
    XML -- YES --> HANDLE{handleId already<br/>on share?}

    HANDLE -- YES --> HANDLE_RET([✅ return handleId<br/>idempotent])
    HANDLE -- NO --> TYPE{record_identifier_type}

    TYPE -- onedata-url --> TYPE_URL[requestPublicHandle = false]
    TYPE -- pid --> TYPE_PID[requestPublicHandle = true]

    TYPE_URL --> POL{identifier_policy}
    TYPE_PID --> POL

    POL -- always-reuse-existing --> AR_PID{dataset.pid<br/>present?}
    AR_PID -- YES --> AR_SET[publicHandleToReuse = pid]
    AR_PID -- NO --> AR_ERR([❌ RecordRequirementError])

    POL -- generate-new-if-missing --> GN_PID{dataset.pid<br/>present?}
    GN_PID -- YES --> GN_SET[publicHandleToReuse = pid]
    GN_PID -- NO --> GN_NONE[publicHandleToReuse omitted]

    POL -- always-generate-new --> AG_NONE[publicHandleToReuse omitted]

    AR_SET --> REG
    GN_SET --> REG
    GN_NONE --> REG
    AG_NONE --> REG

    REG[register_handle<br/>handle_service_id, share_id,<br/>metadata_xml, requestPublicHandle,<br/>publicHandleToReuse]
    REG --> RET([✅ return handle identifier])

    classDef error fill:#E63946,stroke:#9D0208,color:#fff
    classDef success fill:#95D5B2,stroke:#2D6A4F,color:#000
    classDef op fill:#4ECDC4,stroke:#0B7285,color:#000

    class XML_ERR,AR_ERR error
    class HANDLE_RET,RET success
    class REG op
```

### Identifier Policy

<sub>source: `apps/registrar/src/registrar/register/registration.py#_resolve_pid_to_reuse`</sub>

The `identifier_policy` controls `publicHandleToReuse` identically for
both `onedata-url` and `pid` types:

| Policy | `pid` present | `pid` absent |
|--------|---------------|--------------|
| `always-reuse-existing` | `publicHandleToReuse = pid` | Fail with `RecordRequirementError` |
| `generate-new-if-missing` | `publicHandleToReuse = pid` | Omit `publicHandleToReuse` |
| `always-generate-new` | Ignore the PID | Omit `publicHandleToReuse` |

The default is `generate-new-if-missing` — the safest option for batch
runs where some datasets carry PIDs from previous systems and others
are new.
