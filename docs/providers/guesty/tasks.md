# Guesty: tasks

This page describes how pmsbridge lists the tasks in your Guesty account (including cleaning,
maintenance and inspection tasks) and creates a new one, and what the AI assistant can do with them.
Listing is read only. Creating a task changes things in Guesty, so it only works on a connection where an
administrator has turned changes on.

## What the assistant can do

| Tool | What it answers | Needs changes turned on |
|---|---|---|
| `list_tasks` | "Which cleanings are due this week?" A page of tasks with title, kind, state, listing, reservation, due time, checklist (when Guesty gives it) and who it is assigned to (by id, see below). Can be narrowed by listing, state, kind and due dates. | No |
| `create_task` | "Add a deep clean for Beach house before the next arrival." Creates one task and returns it. | Yes |

Before creating, the assistant is told to show you the exact title, kind, listing and due time and get your
confirmation. A task is never created twice by pmsbridge: the request is sent once. If Guesty does not answer
in time, the message says the outcome is unknown and the assistant should read the task list before trying
again, otherwise the team could get two cleaning jobs.

Task titles, descriptions and checklist items are written by people. pmsbridge passes them on only as task
content and tells the assistant, in the tool description, to treat them as information and never as
instructions. They are never written to pmsbridge's log.

## Endpoints used

Guesty's reference pages are at <https://open-api-docs.guesty.com/>, in the Tasks section of the Open API
reference.

| Purpose | Method and path | Parameters | Paging |
|---|---|---|---|
| List tasks | `GET /v1/tasks-open-api/tasks` | `columns` (required), `filters`, `limit` (at least 25), `skip` | `skip` and `limit`: pmsbridge turns the number of rows already read into an opaque cursor. Only an empty page ends the list. |
| Create a task | `POST /v1/tasks-open-api/create-single-task` | JSON body, see below | none |

Every call uses Guesty's shared rate limits and stops after 20 seconds. Reads are repeated safely when Guesty
is briefly unavailable. Creating a task is one request and is not repeated, except when Guesty answers "too
many requests" with a wait time (then it did not process the request). Guesty answers 200 (not 201) with the
task.

Guesty's reference does not state a default or a maximum page size for the task list, so pmsbridge asks for
the size the assistant wants and reads on until Guesty returns an empty page. The last page of a list
therefore costs one extra, empty request.

### How the list is filtered

Guesty documents a filter only for the state (`status` with the `@nin` operator) and for today's tasks. So:

- **State** is sent to Guesty as `{'status':{'@nin':[...]}}`, excluding every other state. pmsbridge checks
  the state again on the rows it gets back, so a filter that Guesty ignored cannot return a wrong task.
- **Listing, kind and due dates** are applied by pmsbridge on the rows Guesty returns. It reads whole pages
  (100 rows) and looks through at most 10 pages (1,000 rows) for each call. A page can therefore hold fewer
  tasks than asked for, or none, with a next cursor still set: the assistant is told to keep passing the cursor
  back until it is empty. Each call continues exactly where the previous one stopped.
- A task with no due time never matches a due date filter. Due dates are compared as UTC dates.

## Fields read from the list

The list answer is a plain array. `columns` is `status taskTitle type listing reservation scheduledFor
endTime canStartAfter mustFinishBefore assignee id`, which is Guesty's example column list plus `type`.
Guesty wraps some values as `{"children": <text>}`; pmsbridge reads either form.

| Shown as | Guesty field | Notes |
|---|---|---|
| id | `id` (list), `_id` (task object) | A row without an id is skipped. |
| title | `taskTitle` (list), `title` (task object) | Shortened with a marker after 500 characters. "Unknown" when missing. |
| kind | `type` | See below. |
| state | `status` | See below. |
| listing | `listing.listingId`, else `apply.listingId` | |
| reservation | `reservation.reservationId` (or plain text), else `apply.reservationId` | Guesty does not describe the shape of `reservation` on a list row, so only these two forms are read. |
| due | `scheduledFor.mustFinishBefore` (task object: `timing.mustFinishBefore`), else the fixed start time | Must carry a time zone offset, otherwise it is left empty. |
| description | `description` | Task object only. |
| checklist | `checklistAggregated[]` (`name`, `finished`), else `checklist[]` with `checklistFinished[]` | Task object only. The person who ticked an item (`by`) is never read. At most 50 items. |
| extra values | `status` as Guesty wrote it, `type` when shown as "other", `priority`, `assigneeId`, `endTime` (when it was finished) | Shown for display only. |

**States.** `pending` and `confirmed` are shown as open, `in progress` as in progress, `completed` as done and
`canceled` as cancelled. Any other word is shown as unknown.

**Kinds.** Guesty's `cleaning`, `maintenance`, `inspection` and `check-in` are shown with the same names. Its
other kinds (`mid-stay`, `runner`, `sanitation`, `issue`, `inventory`, `administration`) are shown as other,
with the original word kept as an extra value. Guesty has no check-out kind.

**Checklist on the list.** Guesty's reference does not list a checklist among the list columns, so tasks from
`list_tasks` normally have an empty checklist. The checklist of a task you just created is returned by
`create_task`, as Guesty answers it.

**Assignee.** A task list row carries the assigned team member's id and full name (`assignee.assigneeFullName`):
the name is shown as the assignee and the id as an extra value. A task object returned by `create_task` has
only the id. Phone numbers, email addresses and pictures of staff are never read.

**List rows.** Guesty wraps each column of a list row in its own object: `{"status": ...}`, `{"type": ...}`,
`{"children": <title>}`, `{"date": <time>}`. Guesty refuses a `limit` below 25, so pmsbridge always asks for at
least 25 rows and returns only as many as the assistant asked for.

## Creating a task

The request body holds only what the assistant gave, using the fields Guesty documents for it:

| Given | Sent as | Notes |
|---|---|---|
| title | `title` | 1 to 200 characters. |
| description | `description` | 1 to 5,000 characters. |
| kind | `type` | `cleaning`, `maintenance`, `inspection` or `check_in` (sent as `check-in`). "Other" is left out so Guesty uses its own default. A check-out or unknown kind is refused and nothing is sent. |
| checklist | `checklist` | A list of text items, at most 50, 1 to 200 characters each. Guesty documents the checklist as a list of strings. |
| listing | `listingId` | Optional. |
| reservation | `reservationId` | Optional. Guesty's reference lists both as optional and states no rule that one of them is required. |
| due time | `canStartAfter` (now) and `mustFinishBefore` | Sent as a flexible window in UTC: the task can start now and must finish before the due time. A due time in the past is refused and nothing is sent. |

The returned task is what Guesty answers. If it does not echo a checklist you sent, the checklist shown is
empty: check the task in Guesty. Priority, assignees, supervisors, attachments, expenses and comments are not
offered.

Things to know:

- Guesty's reference does not say which time zone it assumes for task times, so pmsbridge always sends UTC
  with a trailing `Z`.
- If Guesty accepts the task but its answer cannot be read, the error says the task was probably created and
  to check the list in Guesty first.
- If less than 2 seconds are left before the assistant's deadline, nothing is sent.
- pmsbridge cannot change or delete a task, and cannot tick checklist items: Guesty's index offers no way to
  tick single items.

## What assistants can see

An AI assistant you have approved can read task titles, descriptions, checklists, listing and reservation ids
and due times on the connections you allowed and, if you turned changes on, create tasks in your account. If
that is too much for some assistants, do not approve them for that connection, or leave changes off.

## Known gaps

- **Assignee names**: on the list only, not on a newly created task.
- **Checklist on the list**: not offered by Guesty's list columns.
- **Filters by listing, kind and due date**: done by pmsbridge within 1,000 rows, because Guesty documents no
  such filter.
- **Updating, deleting and reading one task**: not offered.
- **Cleaning schedules, rosters, inspection results and cleaning status changes**: Guesty's reference has no
  such endpoints. Cleaning is handled as tasks of the kind cleaning.
- **Unverified shapes**: the layout of `reservation` on list rows, the time zone of task times, and the page
  size limits are not given in Guesty's reference. The adapter accepts the shapes described above and treats
  any other as an error or as empty.
