# OGAI Annotator — Development Log

Full changelog for the OGAI Annotation Platform from initial deployment
through the current stable release. Entries are reverse-chronological.

**Live URL:** https://ogai-annotator.onrender.com  
**Repository:** https://github.com/ProgramDoc/OGAI_Annotator

---

## v1.6 — Per-field annotation system, form reset fix, span navigation, scoped CSV export (current)

**Date:** 2026-03-29  
**Files changed:** `frontend/annotator.html`, `backend/main.py`

### Overview

Five interconnected improvements driven by annotation UX review: (1) a form
persistence bug when switching PDFs, (2) opaque document-level Confirm/Correct/
Flag buttons replaced with an explicit per-field annotation system, (3) clickable
span tags that navigate the PDF to the evidence location, (4) scoped CSV export
with a project/paper chooser, and (5) an analytics-friendly per-field annotation
column layout in the export.

---

### Fix 1 — Annotation persistence when switching PDFs

**Problem:** Clicking a different paper in the sidebar called `loadPdf()` which
reset `spans` and `pipelinePredictions` but left all form field values populated
from the previous paper. The DOM held stale values; `loadExistingAnnotation` then
layered the new paper's saved values *on top* of the old ones, merging rather
than replacing.

**Fix:** `loadPdf()` now calls `resetForm()` before fetching the new PDF.
`resetForm()` is a comprehensive teardown function that:

- Clears every `[id^="f-"]` input, textarea, and select
- Clears every `[id^="fc-"]` correction textarea and `[id^="ff-"]` flag textarea
- Closes all `.correction-area` and `.flag-area` panels
- Removes all per-field annotation button `.active` classes and resets all
  `.ann-status-dot` back to `none`
- Strips `field-confirmed`, `field-prefilled`, `field-corrected` border classes
- Resets subcategory and study-type cascades to their empty placeholder states
- Hides the type-specific section
- Resets `fieldAnnotations`, `pipelinePredictions`, and `reviewerAction` to
  their initial values
- Resets the autofill button state and paper-level action buttons

---

### Fix 2 — Per-field annotation system (replaces document-level buttons)

The previous three-button row (Confirm / Correct / Flag) operated at the paper
level and was semantically unclear — it was not obvious what "Confirm" meant for
a paper that had 40 fields with mixed AI-populated and empty values.

**New design:** Every field (Layer 1 universal, Layer 2 type-specific, Layer 3
modifier) now has three inline action buttons in its label row:

| Button | Action | Visual |
|--------|--------|--------|
| ✓ Confirm | AI value is correct; no change needed | Green dot, green left border |
| ✎ Correct | AI value is wrong; opens correction sub-area | Orange dot, orange left border |
| ⚑ | Flag this field; opens flag note sub-area | Purple dot (additive with Confirm/Correct) |

**Correction sub-area (`ca-{fieldId}`):** Opens below the main field input.
Contains a free-text textarea and a **← Use AI value** button that pre-populates
it with whatever the AI extracted, so the reviewer can edit from the AI baseline
rather than re-typing from scratch. All values are tracked:

```javascript
fieldAnnotations[fieldId] = {
  status:          'corrected',   // 'confirmed' | 'corrected'
  ai_value:        '...',         // snapshot of pipelinePredictions at click time
  original_value:  '...',         // field value at click time
  corrected_value: '...',         // what the reviewer typed
  flagged:         true | false,
  flag_note:       '...',
}
```

**Flag sub-area (`fa-{fieldId}`):** Opens additively — a reviewer can flag *and*
confirm or flag *and* correct. The flag button is a toggle; deactivating it clears
`flag_note` and closes the panel.

**Status dot (`asd-{fieldId}`):** A 6px circle before each field label shows
annotation state at a glance. Confirmed = green, corrected = orange, flagged-only
= purple. Corrected takes precedence over flagged for the dot colour.

**Annotation summary bar:** A summary line at the top of the bottom bar shows
live counts: `Field annotations: 12 confirmed · 3 corrected · 1 flagged`.

**Paper-level status buttons** (bottom bar): The old Confirm/Correct/Flag buttons
are repurposed as paper-level workflow markers: **✓ Complete**, **✎ Needs Review**,
**⚑ Flag Paper**. These write to `reviewer_action` in `data_json` and do not
interact with per-field state.

**`fieldAnnotations` state is saved in `saveAnnotation()`** as the new
`field_annotations` key in the payload and restored by `setFormData()` on reload,
including reopening open correction and flag panels, restoring status dots,
and re-applying border classes.

**CSS priority rule:** `field-confirmed` (green) is declared *after*
`field-prefilled` and `field-corrected` (amber) in the stylesheet so that
`!important` cascade order gives confirmed fields a green border even when
the row also carries `field-prefilled`. `updateFieldAnnotationUI()` also
removes `field-prefilled` explicitly when setting confirmed or corrected.

---

### Fix 3 — Span tag navigation: click excerpt to jump to PDF location

**Problem:** The purple/coloured span-tag excerpts beneath linked fields showed
the page number but provided no way to navigate there without manually scrolling
the PDF.

**Fix:** Each span tag is now clickable (cursor pointer, `title` tooltip). Clicking
calls `scrollToSpanInPdf(span)` which scrolls the PDF to the correct page and
plays the existing 3× flash animation on the highlight box. The location badge
now shows a 🔍 indicator.

`setActiveField()` also refined: clicking the anchor button on a field that
already has a linked span now enters re-link mode *and* navigates to the span,
rather than requiring a second click.

---

### Fix 4 — Scoped CSV export dialog

**Problem:** The single **⬇ Export CSV** button exported all papers in the user's
library with no scope control. Reviewing a single project required filtering in
pandas.

**Fix:** The button now opens a modal dialog listing three export paths:

| Option | API call | When shown |
|--------|----------|------------|
| All PDFs (library) | `GET /api/export/csv` | Always |
| Current PDF only | `GET /api/export/csv?paper_id={id}` | If a PDF is open |
| Project: {name} | `GET /api/export/csv?project_id={id}` | One per project |

The export endpoint in `main.py` now accepts `paper_id: Optional[int]` and
`project_id: Optional[int]` as query parameters. Export filename adapts to scope:
`ogai_{paperfilename}.csv`, `ogai_project_{name}.csv`, or `ogai_annotations.csv`.

---

### Fix 5 — Analytics-friendly per-field annotation columns in CSV

**Problem:** `corrections_json` and `field_annotations_json` were opaque JSON
blobs in the CSV, unusable without parsing in pandas.

**Fix:** The export function collects all field IDs that appear in any
`field_annotations_json` across the exported rows and generates five flat columns
per annotated field:

```
{field_id}__ann_status        confirmed | corrected | (empty)
{field_id}__ai_value          what the AI extracted
{field_id}__corrected_value   what the reviewer typed (blank if confirmed)
{field_id}__flagged            Yes | (empty)
{field_id}__flag_note         reviewer's flag text
```

These columns are appended after all existing `FLAT_COLS`. The set is determined
dynamically from the actual data in each export scope, so a project with only
diagnostic accuracy studies will only have columns for fields those studies
actually used.

The raw `field_annotations_json` blob is also retained as a column for
round-tripping.

---

### Backend schema changes (`main.py`)

New column in `annotations` table:

| Column | Type | Description |
|--------|------|-------------|
| `field_annotations_json` | `TEXT` | Per-field annotation states (JSON object) |

Migration added to `init_db()`:

```python
"ALTER TABLE annotations ADD COLUMN field_annotations_json TEXT",
```

`AnnotationPayload` extended:

```python
class AnnotationPayload(BaseModel):
    data: dict[str, Any] = {}
    spans: list[dict[str, Any]] = []
    field_annotations: dict[str, Any] = {}  # new
```

`save_annotation` stores `field_annotations` in the new column and includes it in
the `ON CONFLICT DO UPDATE` clause. `get_annotations` returns it in the `data`
dict for the frontend to restore on reload.

---

### Known issues resolved in this version

- ~~Layer 2 fields in CSV: only available via `data_json` parsing~~ — still applies for
  type-specific *values*, but per-field *annotation states* for all layers are
  now flat columns.
- ~~Confirm/Correct/Flag semantics unclear~~ — replaced with per-field system.



**Date:** 2026-03-29  
**Files changed:** `backend/main.py`

### Overview

Full rewrite of `backend/main.py`. Driven by a persistent 500 error on PDF
upload caused by the production database having been created with a legacy
`TEXT PRIMARY KEY` schema — a problem that accumulated across multiple
partial migrations until a clean ground-up rewrite was the only reliable fix.

### Legacy schema problem — root cause

The original database (created at v1.0) used `id TEXT PRIMARY KEY` on the
`papers` table, storing a sha256-derived string as the row identifier. SQLite
only auto-generates primary key values for `INTEGER PRIMARY KEY` columns. For
any other type — including `TEXT PRIMARY KEY` — the application must supply
the `id` value explicitly.

Every version of the backend from v1.1 through v1.4.x inserted papers without
providing an `id` value, leaving it `NULL`. `cur.lastrowid` (the insert
cursor's row identifier) returns SQLite's internal `rowid` — an unrelated
integer that is *not* stored in the `id` column. Queries using `WHERE id=8`
found nothing, because the row had `id=NULL`, not `id=8`.

The additional complexity: the production database also had `file_path TEXT
NOT NULL` and `upload_time TEXT NOT NULL` columns (from the original schema)
that the v1.3+ code never populated, causing every `INSERT` to be silently
discarded by `OR IGNORE` due to NOT NULL constraint violations.

### Fix — startup schema detection and table rebuild

`init_db()` now:

1. Inspects the `papers` table using `PRAGMA table_info` to detect whether
   `id` is `TEXT` type (legacy) or `INTEGER` (correct)
2. If legacy: renames the table to `papers_legacy_backup`, creates a new
   `papers` table with `INTEGER PRIMARY KEY AUTOINCREMENT`, and re-inserts
   all existing rows using their SQLite `rowid` as the new integer `id`
3. If correct (fresh install): uses `CREATE TABLE IF NOT EXISTS` normally

This migration runs on every startup but only does work the first time (once
the table is `INTEGER PRIMARY KEY`, detection returns false).

### Fix — `_col_info()` helper for runtime schema discovery

A new `_col_info(conn, table)` helper reads `PRAGMA table_info` at runtime to
return `{col_name: {notnull, dflt}}` for every column. The upload endpoint
uses this to:

- Build `INSERT` statements that only reference columns that exist
- Automatically supply fallback values for any `NOT NULL, no-default` column
  found in the live schema (handles `file_path`, `upload_time`, and any other
  legacy columns gracefully)
- Build `SELECT` statements that only reference columns that exist

### Fix — HEAD method on root route

Render's health check uses `HEAD /`. FastAPI does not automatically handle
`HEAD` for `GET` routes in newer Starlette versions. The root route now uses
`@app.api_route("/", methods=["GET", "HEAD"])`.

### Other changes in this version

- `PRAGMA foreign_keys=OFF` set on every connection during startup migration
  to avoid FK constraint failures while rebuilding the papers table
- `logging.basicConfig(level=logging.ERROR)` configured at module level so
  diagnostic `logger.error()` calls appear in Render logs
- All DB write operations use explicit `conn.execute()` + `conn.commit()`
  with no `with conn:` context manager — avoids the double-commit issue that
  caused silent write failures in earlier versions
- `papers_legacy_backup` table retained on disk as a safety net after rebuild

---

## v1.4.x — Legacy DB migration hotfixes

**Date:** 2026-03-26 to 2026-03-28  
**Files changed:** `backend/main.py` (multiple incremental patches)

A series of hotfixes applied while diagnosing the upload 500 error. Each
fix resolved one symptom while revealing the next layer of the underlying
schema mismatch. Documented here as a complete audit trail.

### DB migration rule established during this series

> **Any column in a `CREATE TABLE IF NOT EXISTS` schema definition must
> simultaneously appear as an `ALTER TABLE` migration in the migration list.**

`CREATE TABLE IF NOT EXISTS` is a no-op when the table already exists. The
migration list (`ALTER TABLE ... ADD COLUMN`) is the only mechanism that
adds columns to existing databases. If a column is missing from the migration
list, queries against that column fail on any database that predates the
column's addition to the schema.

Going forward, the migration list in `init_db()` is the single source of
truth for the live DB schema. Every migration is wrapped in
`try/except sqlite3.OperationalError: pass` — safe to run on any database
regardless of age.

### v1.4.1 — `no such column: sha256` in `list_papers`

`sha256` was not in the migration list. Fixed by adding the migration and
removing `sha256` from the `list_papers` SELECT (the frontend never uses it).

### v1.4.2 — `no such column: created_at` in `list_papers`

`created_at` was added to the migration list but `ORDER BY created_at`
executed before the migration was guaranteed to have run. Fixed by replacing
all `ORDER BY created_at` with `ORDER BY id` throughout — `id` is
`AUTOINCREMENT` and gives identical ordering while always existing.

### v1.4.3 — upload 500 from `with conn:` + `conn.commit()` double-commit

`upload_paper` and other write endpoints used Python's `with conn:` context
manager (which auto-commits on exit) *and* called `conn.commit()` manually
inside the block. Under newer Starlette/anyio thread execution this left the
connection in an indeterminate state where a subsequent SELECT ran in a new
transaction before the write was visible, returning `None`.

Fixed by removing all `with conn:` wrappers from DML operations. All write
endpoints now use explicit `conn.execute()` + `conn.commit()` only.

### v1.4.4 — `no such column: disk_filename` crashes uncaught INSERT

The `disk_filename` column was not in the migration list. The `except
sqlite3.IntegrityError` block did not catch `sqlite3.OperationalError`
(column missing), so missing columns fell through to an unhandled 500.

Fixed by adding `disk_filename` to the migration list and rewriting
`upload_paper` with cascading INSERT fallbacks that catch `OperationalError`
separately from `IntegrityError`.

### v1.4.5 — `INSERT OR IGNORE` silently discarded by `NOT NULL` constraint

`PRAGMA table_info` revealed `file_path NOT NULL` and `upload_time NOT NULL`
columns in the production DB — from the original v1.0 schema. Every INSERT
that omitted these columns was silently discarded by `OR IGNORE`.

Fixed by reading `PRAGMA table_info` at runtime to discover all `NOT NULL,
no-default` columns and supplying fallback values for them automatically:
`file_path` receives the disk filename, `upload_time` receives the current
timestamp, and any other unknown NOT NULL columns receive `""`.

### v1.4.6 — `INSERT OR IGNORE` silently discarded by `UNIQUE(sha256)` constraint

The production DB had `UNIQUE(sha256)` — a uniqueness constraint on sha256
alone, not `UNIQUE(sha256, user_id)`. Papers uploaded pre-auth had
`user_id=NULL`. When the same file was re-uploaded, `OR IGNORE` silently
discarded the INSERT. The follow-up `SELECT WHERE sha256=? AND user_id=1`
found nothing because the row still had `user_id=NULL`.

Fixed by issuing unconditional `UPDATE` statements after the INSERT to stamp
`user_id` and `disk_filename` onto the row regardless of whether the INSERT
actually fired.

### v1.4.7 — `id=None` returned after successful INSERT

After the INSERT and UPDATE, the SELECT used `WHERE sha256=?` and returned
a row — but `row["id"]` was `NULL` because the column was never populated
(the `TEXT PRIMARY KEY` schema issue). `cur.lastrowid` was returned as the
`id` in the API response. The frontend correctly received integer ids (8, 9,
10...) but `get_pdf` queried `WHERE id=8` which found nothing, because the
column value was `NULL`.

This was the point at which patching was abandoned in favour of the complete
v1.5 rewrite.

---

## v1.4 — User authentication and user-scoped data

**Date:** 2026-03-26  
**Files changed:** `backend/main.py`, `frontend/annotator.html`, `frontend/login.html` *(new)*

### New: `frontend/login.html`

Standalone login page at `/login`:
- **Register tab** — display name, email, password (min 8 chars); auto-signs in
- **Sign In tab** — email + password; 30-day HTTP-only session cookie
- **Admin access** — expands a secret key field; entering `ADMIN_SECRET`
  signs in as admin, bypassing registration
- Pre-checks `GET /api/auth/me` and redirects to `/` if already authenticated

### Password security

- PBKDF2-SHA256, 260,000 iterations, 32-byte random salt per password
- `hmac.compare_digest` used for all secret comparisons (timing-safe)
- Passwords never stored or logged in plaintext

### Session management

```python
SESSION_COOKIE = "ogai_session"
SESSION_DAYS   = 30
```

Sessions stored in the `sessions` table with `expires_at`. Cookie flags:
`HttpOnly`, `SameSite=Lax`, `Secure` on Render (detected via `RENDER` env var).

### New DB tables

```sql
CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT    NOT NULL,
    password_hash TEXT    NOT NULL,
    password_salt TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'reviewer',
    created_at    TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE sessions (
    token      TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    DEFAULT (datetime('now')),
    expires_at TEXT    NOT NULL
);
```

### User-scoped data

All endpoints filter papers and projects by `user_id`. Reviewer ID is set
automatically from `user.display_name` on save — the manual reviewer ID
input is removed from the frontend.

### New auth endpoints

`POST /api/auth/register`, `POST /api/auth/login`, `POST /api/auth/admin`,
`POST /api/auth/logout`, `GET /api/auth/me`

### `annotator.html` changes

- Reviewer ID text input removed from top bar
- User display name + email shown in top bar
- Sign out button added
- `saveAnnotation()` no longer sends `reviewer_id` in payload
- `loadExistingAnnotation()` no longer passes `reviewer_id` query param
- On load, `GET /api/auth/me` called; 401 redirects to `/login`

### New environment variables

`ADMIN_SECRET` (required), `ADMIN_EMAIL` (optional), `ADMIN_NAME` (optional)

---

## v1.3 — Project folders, paper deletion, upload race condition fix

**Date:** 2026-03-26  
**Files changed:** `frontend/annotator.html`, `backend/main.py`

### Project folder sidebar

Flat paper list replaced with collapsible project folders:
- **+** button creates a named project
- Collapse/expand by clicking folder header; state held in `collapsedProjects` Set
- Rename (✎) and delete (✕) on folder hover; delete unassigns papers
- **Move to…** dropdown on paper hover
- **Delete paper** (✕) on paper hover; cascades annotations and spans
- Papers with no project appear in **Unassigned**

### PDF upload — `FileList` race condition fix

**Problem:** `onchange="uploadFiles(this.files)"` passes a live `FileList` to
an `async` function. When the first `await` suspends execution, some browsers
(Chrome in particular) clear the `FileList` because focus has returned to the
page. The upload loop then iterates an empty list silently.

**Fix:**

```javascript
// Removed: onchange="uploadFiles(this.files)"

// Added in init block:
document.getElementById('file-input').addEventListener('change', function () {
  uploadFiles(this.files);
});

async function uploadFiles(files) {
  const fileArray = Array.from(files);  // snapshot BEFORE first await
  ...
}
```

Additional upload improvements: "Uploading…" feedback in upload zone during
fetch; per-file error alerts with server detail; file input value reset after
upload so the same file can be re-selected; uploaded paper auto-selected and
opened on success.

### Field-select → PDF scroll and flash

Clicking the **anchor** button on a field that already has a linked span
now scrolls the PDF to that page and plays a 3× flash animation on the
highlight box (`@keyframes spanFlash`). Span overlay boxes gained
`data-field` attribute so `flashSpanBox()` can find the right box by field.

### Icon cleanup

Save button: `💾 Save` → `Save`. Auto-fill button: `🤖 Auto-fill` → `Auto-fill from PDF`.

### New backend endpoints

`GET/POST/PUT/DELETE /api/projects`, `POST /api/papers/{id}/assign`,
`DELETE /api/papers/{id}` (cascades annotations, spans, and PDF file)

---

## v1.2 — Taxonomy v2.1, AI prefill, corrections tracking

**Date:** 2026-03-26  
**Files changed:** `frontend/annotator.html`, `backend/main.py`

### Frontend renamed

`frontend/index.html` → `frontend/annotator.html` to avoid collision with
the taxonomy site (`ProgramDoc/StudyTaxonomy`). `main.py` updated to serve
explicitly by name.

### Taxonomy v2.1 alignment

The major category / subcategory / study type cascade corrected to match the
v2.1 taxonomy tree (33 study types, 5 major categories).

**Major categories — before vs after:**

| Before | After |
|--------|-------|
| Primary Studies (included observational) | Primary Studies (experimental only) |
| *(missing)* | **Observational Studies** |
| Evidence Synthesis | Evidence Synthesis |
| Guidance / Consensus | Guidance / Consensus |
| Economic & Decision Models | Economic & Decision Models |

**SUBCATS mapping (corrected):**

```javascript
"Primary Studies":       ["Randomized Controlled","Non-Randomized Controlled",
                          "Non-Randomized Uncontrolled","Quasi-Experimental",
                          "Qualitative & Mixed Methods"],
"Observational Studies": ["Descriptive","Analytical","Diagnostic / Prognostic"],
"Evidence Synthesis":    ["Reviews"],
"Guidance / Consensus":  ["Guidelines & Consensus"],
"Economic & Decision Models": ["Economic Evaluation"],
```

ROB_MAP corrections: `ROBINS-I V2` for DiD and RD; `ROBINS-E (adapted)` for
Case-Crossover (was `NOS`). Case-Crossover `TYPE_FIELDS` entry added (9 fields).

### AI auto-fill (`POST /api/papers/{id}/prefill`)

Sends the PDF (base64) to the Anthropic API with a study-type-keyed extraction
prompt. Returns `{field_id: value}` JSON.

- **Transport:** stdlib `urllib` only — no new pip dependencies
- **Threading:** `asyncio.to_thread` prevents blocking the event loop
- **PDF API header:** `anthropic-beta: pdfs-2024-09-25` required for document blocks
- **JSON safety:** response stripped of accidental markdown fences before parsing

**UI:** Amber panel between Stage 1 and Layer 1. Button disabled until both
paper and study type are selected. Fields receive amber left border on fill;
border clears on first edit. Re-fill button available after first extraction.

### Structured corrections tracking

Three columns added to `annotations`:

| Column | Description |
|--------|-------------|
| `correction_notes` | Free-text reviewer explanation |
| `corrections_json` | Structured diff `{field: {from, to}}` vs AI baseline |
| `pipeline_predictions_json` | Full AI extraction snapshot |

Visual feedback: `Correct` / `Flag` action turns changed fields red. Summary
restored on page reload.

---

## v1.1 — Initial deployment to Render.com

**Date:** 2026-03-25

- `backend/main.py` path resolution updated for Render persistent disk via
  `RENDER_DATA_DIR` env var (falls back to repo root locally)
- `render.yaml` added for Blueprint deployment
- `.gitignore` updated to exclude `papers/` and `annotations.db`
- Frontend renamed from `index.html` to `annotator.html`

---

## Known issues / future work

- **`corrections_json` baseline gap.** If a reviewer fills fields without
  using auto-fill, `pipelinePredictions` is empty and `corrections_json` is
  always `{}`. Future: pre-populate baseline from the pipeline's Stage 2
  extraction CSV at upload time.

- **Session expiry UX.** Expired sessions silently redirect to `/login`
  mid-session. A proactive warning banner before expiry would improve UX.

- **Admin user management.** No UI to list registered users, reset passwords,
  or revoke sessions. Requires direct SQLite access.

- **Layer 2 field values in CSV.** Type-specific field *values* are stored in
  `data_json` and not individually flat-exported. Per-field annotation *states*
  for all layers are now flat (v1.6), but reading the extracted values for e.g.
  `randomization_method` still requires parsing `data_json` in SQLite or pandas.

- **IRR on per-field corrections.** Agreement rate on which fields required
  correction across reviewers (from `field_annotations_json`) is not yet
  computed in-platform. The flat `{field}__ann_status` columns in the CSV
  export make this straightforward in pandas.

- **AI span location logging.** The LLM extraction (`prefill`) returns field
  values but not page/position metadata, so AI-extracted fields cannot be
  auto-linked to PDF locations. Span linking remains manual. A future version
  could prompt the model to return citation snippets and match them against the
  PDF text layer.

---

## DB migration rules

> **Any column in a `CREATE TABLE IF NOT EXISTS` schema block must also appear
> as an `ALTER TABLE` migration in `init_db()`.**

`CREATE TABLE IF NOT EXISTS` skips the statement entirely when the table
already exists. The `ALTER TABLE` migration list is the only mechanism for
adding columns to existing databases. These two must always be kept in sync.

Every migration must be idempotent:

```python
for migration in [...]:
    try:
        conn.execute(migration)
    except sqlite3.OperationalError:
        pass  # column already exists — safe to ignore
```

Do not use `with conn:` combined with explicit `conn.commit()`. Use one or
the other — the recommended pattern throughout v1.5 is explicit
`conn.execute()` + `conn.commit()` with no context manager.

---

## Dependency notes

`backend/requirements.txt` has been unchanged since v1.0:

```
fastapi>=0.110.0
uvicorn[standard]>=0.29.0
python-multipart>=0.0.9
```

No additional packages required. Auth (PBKDF2), AI calls (urllib), and CSV
export all use Python stdlib.
