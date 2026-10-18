# Pterodactyl calls NodeRings makes

This is the panel contract the operator and the metrics exporter use today. A legacy panel has to keep these endpoints, bodies, and response shapes. NodeRings does not call the panel from the API process. The operator on the provider cluster does, with two keys from the operator secret:

| Key | Header | Used for |
| --- | --- | --- |
| Application key | `Authorization: Bearer <application key>` | Creating and deleting servers, nests, eggs, nodes, allocations, suspend |
| Client key | `Authorization: Bearer <client key>` | Everything a server owner does: power, files, databases, schedules, backups |
| Wings node token | `Authorization: Bearer <token>` or `Bearer <token_id>.<token>` | `GET /api/system` on the Wings daemon |

Every panel request sends `Accept: Application/vnd.pterodactyl.v1+json`. JSON bodies use `Content-Type: application/json`. `{identifier}` is the short client-API id (not the numeric server id). `{server_id}` is the numeric application-API id. The panel user id is configured (`PTERODACTYL_PANEL_USER_ID`). NodeRings does not create panel users.

## Application key permissions

The application key is created in the panel admin API screen. Its owner must be a root admin. A non-root account is rejected before any resource check, with `This account does not have permission to access the API.` A missing checkbox on a route we call returns `This action is unauthorized.`

Pterodactyl checks one resource per request. `POST /servers` checks Servers write only. The `user` field and `feature_limits.databases` in that body do not check Users or Server Databases.

| Resource | Read | Write | Why |
| --- | --- | --- | --- |
| Servers | yes | yes | List and look up a server. Create, delete, suspend, unsuspend, and change startup |
| Nodes | yes | no | Deployable check, maintenance flag, node record, and Wings configuration |
| Allocations | yes | no | List free ports. Create sends an existing allocation id and does not create allocations |
| Nests | yes | no | List nests for the game catalog |
| Eggs | yes | no | List eggs, and read the egg used at create |
| Users | no | no | No `/users` call. Create only sends the configured panel user id |
| Locations | no | no | Not called |
| Database Hosts | no | no | Not called. Hosts are configured in the panel admin UI |
| Server Databases | no | no | Not called. Databases are created with the client key |

The client key has no resource checkboxes. It must belong to `PTERODACTYL_PANEL_USER_ID`, and that user must own the servers. Listing, creating, rotating, and deleting a database are client calls (`database.read`, `database.create`, `database.update`, `database.delete`). The server owner already has those. A database host must exist on the panel or create fails later; that is panel setup, not a key checkbox.

List endpoints that the operator pages through must return `meta.pagination.total_pages`, `page`, and `per_page`. Each row is `{ "attributes": { ... } }`.

## Application API

Base path: `{panel}/api/application`

| Method | Path | What we send | Why | What we read |
| --- | --- | --- | --- | --- |
| GET | `/nests?include=eggs.variables&per_page=100&page=` | | Build the game catalog in one list | Nest `id`, `name`. Eggs in `attributes.relationships.eggs.data[].attributes` (`id`, `nest`, `name`) and each egg's `relationships.variables`. If eggs are missing, or they came back without variables, we fall back to the next row once per nest |
| GET | `/nests/{nest_id}/eggs?include=variables&per_page=100&page=` | | Fallback for one nest, not one call per egg | Egg `id`, `nest`, `name`, and `relationships.variables` |
| GET | `/nests/{nest_id}/eggs/{egg_id}?include=variables` | | Fill startup, image, and environment before create | `startup`, `docker_image`, `relationships.variables.data[].attributes.env_variable` and `default_value` |
| GET | `/nodes/{node_id}/allocations?per_page=100&page=` | | Pick a free port when ordering | `id`, `assigned`, `ip`, `port`, `alias` |
| GET | `/nodes/{node_id}` | | Find the Wings address for dedicated CPU | `fqdn`, `daemon_listen`, `scheme` |
| GET | `/nodes/{node_id}/configuration` | | Wings auth for `GET /api/system` | `token_id`, `token` |
| GET | `/nodes?per_page=100` | | Exporter only: node list for capacity | Node `id`, `name`, `fqdn`, `daemon_listen`, `scheme`, memory and disk fields, `maintenance_mode` |
| GET | `/nodes/deployable?memory=&disk=` | `memory` and `disk` in MiB. Page is omitted so the panel returns every match | Exporter asks with `memory=0` and `disk=0` and emits `ptero_node_deployable`. The operator asks with the server's memory and disk before create. A 400 means no node qualifies | Node `id`. A node in this list is deployable. Maintenance is a separate flag |
| GET | `/servers?per_page=100&page=&filter[node_id]={node_id}` | | Dedicated CPU: see which threads are already pinned. Exporter lists servers without the filter and currently reads page 1 only | `identifier`, `external_id`, `node`, `limits.threads` |
| GET | `/servers/{server_id}` | | Install progress, and again after create | `status`: `null` once installed, `"installing"` while it runs, `"install_failed"` on failure. Also `identifier` |
| GET | `/servers/external/{external_id}?include=allocations` | | Find the server for an order. 404 means it was never created | Server attributes, plus `relationships.allocations.data[].attributes` (`id`, `ip`, `port`, `alias`) and `allocation` (primary id) |
| POST | `/servers` | See create body below | Order a game server | Created server attributes, including numeric `id` and `identifier` |
| POST | `/servers/{server_id}/suspend` | Empty body | Suspend the server | 2xx, no body required |
| POST | `/servers/{server_id}/unsuspend` | Empty body | Unsuspend the server | 2xx, no body required |
| DELETE | `/servers/{server_id}` | No query string | Delete the server when the order is removed | 2xx |

### Create server body

```json
{
  "name": "server name",
  "user": 1,
  "egg": 1,
  "docker_image": "image from the egg",
  "startup": "startup command from the egg",
  "environment": { "SERVER_NAME": "default or override" },
  "limits": { "memory": 512, "swap": 0, "disk": 1024, "io": 500, "cpu": 100, "threads": "0,1" },
  "oom_disabled": true,
  "feature_limits": { "databases": 1, "allocations": 1, "backups": 1 },
  "allocation": { "default": 123 },
  "external_id": "NR-{noderings game server uuid}",
  "start_on_completion": true,
  "skip_scripts": false
}
```

`limits.threads` is sent only for a dedicated-CPU plan. `environment` is the egg defaults, with caller values on top. An empty override does not wipe a non-empty egg default. A missing required variable must still be rejected by the panel.

Memory, swap, and disk are MiB. CPU is a percent (100 = one core). `feature_limits.allocations` is the extra ports plus the primary one.

## Client API

Base path: `{panel}/api/client`

`{id}` below is `{identifier}`.

### Power, resources, startup, rename, reinstall

| Method | Path | What we send | Why | What we read |
| --- | --- | --- | --- | --- |
| POST | `/servers/{id}/power` | `{"signal":"start"\|"stop"\|"restart"\|"kill"}` | Power buttons | 2xx |
| GET | `/servers/{id}/resources` | | Overview stats and the metrics exporter | `current_state`. `resources.cpu_absolute`, `memory_bytes`, `disk_bytes`, `network_rx_bytes`, `network_tx_bytes`, `uptime` |
| GET | `/servers/{id}/startup` | | Startup tab | `meta.startup_command`, `meta.raw_startup_command`, `data[].attributes` (`env_variable`, `name`, `description`, `server_value`, `is_editable`, `rules`) |
| PUT | `/servers/{id}/startup/variable` | `{"key":"ENV_NAME","value":"..."}` | Save one startup variable | Updated variable attributes |
| POST | `/servers/{id}/settings/rename` | `{"name":"...","description":""}` | Rename | 2xx |
| POST | `/servers/{id}/settings/reinstall` | Empty body | Reinstall. We then poll application `GET /servers/{server_id}` until `status` is null | 2xx |

### Files

Paths stay inside the server root. `..` is rejected before the call.

| Method | Path | What we send | Why | What we read |
| --- | --- | --- | --- | --- |
| GET | `/servers/{id}/files/list?directory=` | | File manager | `data[].attributes`: `name`, `mode`, `mode_bits`, `size`, `is_file`, `is_symlink`, `mimetype`, `created_at`, `modified_at` |
| POST | `/servers/{id}/files/create-folder` | `{"root":"/","name":"folder"}` | New folder | 2xx |
| POST | `/servers/{id}/files/write?file=` | Raw file bytes, `Content-Type: text/plain` | Save an edited file | 2xx |
| GET | `/servers/{id}/files/contents?file=` | | Read a file in the editor. If the panel says the file is too large, we use the download URL instead | Raw file bytes |
| GET | `/servers/{id}/files/download?file=` | | Download, and the fallback for a large read | `attributes.url` signed URL. We GET that URL with no panel key |
| GET | `/servers/{id}/files/upload` | | Upload | `attributes.url` signed URL. We POST `multipart/form-data` field `files` to `{url}&directory=` |
| POST | `/servers/{id}/files/copy` | `{"location":"/path"}` | Copy | 2xx |
| POST | `/servers/{id}/files/delete` | `{"root":"/","files":["a.txt"]}` | Delete | 2xx |
| PUT | `/servers/{id}/files/rename` | `{"root":"/","files":[{"from":"a","to":"b"}]}` | Rename | 2xx |
| POST | `/servers/{id}/files/chmod` | `{"root":"/","files":[{"file":"a","mode":"644"}]}` | Permissions | 2xx |
| POST | `/servers/{id}/files/compress` | `{"root":"/","files":["a"]}` | Zip | Archive attributes, including `name` |
| POST | `/servers/{id}/files/decompress` | `{"root":"/","file":"a.tar.gz"}` | Unzip | 2xx |

### Databases

| Method | Path | What we send | Why | What we read |
| --- | --- | --- | --- | --- |
| GET | `/servers/{id}/databases` | | Database list | `name`, `username`, `host`, `port`, `remote`, `max_connections` |
| POST | `/servers/{id}/databases` | `{"database":"name","remote":"%"}` | Create. Password must come back on this response | Same fields, plus the password |
| POST | `/servers/{id}/databases/{database_id}/rotate-password` | Empty body | Rotate | New password on the response |
| DELETE | `/servers/{id}/databases/{database_id}` | | Delete | 2xx |

On panel 1.15 the password is nested at `attributes.relationships.password.attributes.password`, not beside `attributes`. A legacy panel can use either place. We check the nested one first.

### Schedules

Cron fields are strings. `*` means every. Task `action` is `command`, `power`, or `backup`. For `power`, `payload` is `start`, `stop`, `restart`, or `kill`. For `backup`, `payload` is empty. `time_offset` is 0–900 seconds. `sequence_id` is sent only when the user reorders a task. Leaving it out must leave the order unchanged.

| Method | Path | What we send | Why | What we read |
| --- | --- | --- | --- | --- |
| GET | `/servers/{id}/schedules` | | Schedule list | Schedule attributes, and tasks. Tasks may be a sibling `relationships.tasks.data[].attributes` or nested under `attributes.relationships` |
| POST | `/servers/{id}/schedules` | `name`, `is_active`, `only_when_online`, `minute`, `hour`, `day_of_month`, `month`, `day_of_week` | Create | Schedule attributes, including `id` |
| POST | `/servers/{id}/schedules/{schedule_id}` | Same fields as create | Edit | Updated attributes |
| DELETE | `/servers/{id}/schedules/{schedule_id}` | | Delete | 2xx |
| POST | `/servers/{id}/schedules/{schedule_id}/execute` | Empty body | Run now | 2xx |
| POST | `/servers/{id}/schedules/{schedule_id}/tasks` | `action`, `payload`, `time_offset`, `continue_on_failure` | Add a task | Task `id`, `sequence_id`, `action`, `payload`, `time_offset`, `continue_on_failure` |
| POST | `/servers/{id}/schedules/{schedule_id}/tasks/{task_id}` | Same fields, plus `sequence_id` only when reordering | Edit a task | Updated task attributes |
| DELETE | `/servers/{id}/schedules/{schedule_id}/tasks/{task_id}` | | Delete a task | 2xx |

Schedule fields we show: `name`, `cron` (`minute`, `hour`, `day_of_month`, `month`, `day_of_week`) or the same fields flat on the object, `is_active`, `only_when_online`, `is_processing`, `last_run_at`, `next_run_at`. `last_run_at` is when a run started. The panel does not return a success flag.

### Network and backups

| Method | Path | What we send | Why | What we read |
| --- | --- | --- | --- | --- |
| GET | `/servers/{id}/network/allocations` | | Network tab | `id`, `ip`, `port`, `ip_alias`, `notes`, `is_default` |
| POST | `/servers/{id}/network/allocations` | Empty body | Assign another allocation the node already has | New allocation attributes |
| POST | `/servers/{id}/network/allocations/{allocation_id}` | `{"notes":"..."}` | Notes | Updated attributes |
| POST | `/servers/{id}/network/allocations/{allocation_id}/primary` | Empty body | Make it the primary | 2xx |
| DELETE | `/servers/{id}/network/allocations/{allocation_id}` | | Remove an extra allocation. The primary must stay | 2xx |
| GET | `/servers/{id}/backups` | | Backup list | `uuid`, `name`, `bytes`, `is_successful`, `is_locked`, `created_at`, `completed_at` |
| POST | `/servers/{id}/backups` | `{"name":"...","is_locked":false}` | Create | Backup attributes |
| POST | `/servers/{id}/backups/{backup_uuid}/lock` | Empty body | Toggle the lock | 2xx |
| POST | `/servers/{id}/backups/{backup_uuid}/restore` | `{"truncate":true}` or `{"truncate":false}` | Restore. `truncate` deletes existing files first | 2xx |
| GET | `/servers/{id}/backups/{backup_uuid}/download` | | Download | `attributes.url` |
| DELETE | `/servers/{id}/backups/{backup_uuid}` | | Delete | 2xx |

### Console

| Method | Path | What we send | Why | What we read |
| --- | --- | --- | --- | --- |
| GET | `/servers/{id}/websocket` | | Open the console. The browser never sees this response | `token` and `socket`, under `data` or `attributes` |

We then open the Wings socket from `socket`, send `{"event":"auth","args":["<token>"]}`, and after auth success send `{"event":"send logs","args":[null]}` so the scrollback arrives. Commands the customer types are forwarded as Wings events. We do not call `POST /servers/{id}/command`.

## Wings

These are on the node daemon (`scheme://fqdn:daemon_listen`), not on the panel.

| Method | Path | What we send | Why | What we read |
| --- | --- | --- | --- | --- |
| GET | `/api/system` | Bearer node token. If that is 401 or 403, retry `Bearer {token_id}.{token}` | Dedicated CPU core count | `cpu_count` |
| GET | signed upload or download URL | No panel key | File transfer after the client API returns the URL | File bytes, or 2xx on upload |
| WebSocket | URL from `/websocket` | Auth event, then `send logs`, then relayed console events | Live console | `console output`, `status`, `jwt error`, `daemon error` |
