# Pseudo

**Build in public. Stay private.**

Pseudo is a pseudonymous proof-of-work platform for developers. It brings projects, timed build sessions, accomplishments, and selected GitHub evidence into a persistent build history that developers control.

Building publicly often means spending time writing updates, chasing engagement, or tying every project to a real identity. Pseudo focuses on documenting the work itself: record a session, describe what you accomplished, attach relevant evidence, and decide whether to publish it.

This is a learning and portfolio project with a working local MVP, built incrementally to explore full-stack development, relational data modeling, authentication, OAuth, and privacy boundaries.

## Current features

- Register and log in with an email, password, and unique pseudonym.
- Create projects and track one active work session per account.
- Finish sessions with an optional accomplishment; timing is calculated by the backend.
- View a chronological Build Log and totals calculated from completed sessions.
- Keep sessions private by default, with explicit publish/unpublish controls.
- Share a public profile at `/u/:pseudonym`; public totals include only published sessions.
- Connect a GitHub account with OAuth, browse public repositories, and optionally link a repository to a project.
- Retrieve recent commits and pull requests, then explicitly attach selected items to a completed session.
- Display eligible GitHub evidence on published entries without exposing account email or credentials.
- Persist accounts, projects, sessions, repository links, and evidence in PostgreSQL across server restarts.

GitHub remains the source of code. Pseudo does not copy repositories, post automatically, or treat commits as proof of hours worked.

## Tech stack

| Layer | Technology |
| --- | --- |
| Frontend | React, JavaScript, Vite, CSS |
| Backend | Python, FastAPI, Pydantic |
| Database | PostgreSQL, SQLAlchemy, Alembic migrations |
| Authentication | Argon2id password hashing, server-side sessions, HTTP-only cookies |
| GitHub integration | Authlib OAuth with PKCE, HTTPX, Fernet credential encryption |
| Verification | Python unittest, FastAPI TestClient, real PostgreSQL integration tests, mocked GitHub HTTP |

## Architecture

```mermaid
flowchart LR
    A[Browser / React] -->|HTTP JSON + account cookie| B[FastAPI]
    B --> C[Validation and ownership checks]
    C --> D[SQLAlchemy]
    D --> E[(PostgreSQL)]
    B -->|OAuth and REST requests| F[GitHub]
    E -->|Stored records| B
    B -->|Safe JSON response| A
```

During local development, Vite proxies API paths to FastAPI. Each request receives a SQLAlchemy session; writes commit explicitly and the connection returns to the pool when the request ends. Project and session operations enforce ownership on the backend.

```text
User -> Projects -> BuildSessions -> Selected GitHubEvidence
User -> GitHubConnection
Project -> optional GitHub repository ID
```

Public profiles select only completed, published sessions. Before including GitHub evidence, the backend rechecks repository visibility and requires an explicit public result. Private or unverifiable evidence is omitted. Access credentials never reach the frontend.

## Repository layout

```text
frontend/
  src/                  React dashboard, authentication and public-profile UI
  vite.config.js        Local API proxy
backend/
  main.py               Project, session and public-profile routes
  auth.py               Accounts and cookie sessions
  github_integration.py OAuth, repositories and evidence
  models.py             SQLAlchemy models
  database.py           Environment configuration and database sessions
  migrations/           Alembic schema changes
  test_main.py          Application and persistence tests
  test_github.py        GitHub integration and privacy tests
  .env.example          Placeholder configuration; no real credentials
```

## Run locally

Requirements: Python 3.11+, Node.js 22.12+ and npm, and PostgreSQL 17 or 18. Start PostgreSQL before starting the backend. The application does not substitute SQLite if PostgreSQL is unavailable.

Commands below assume the project has been cloned or downloaded and the terminal is in its root folder. Do not overwrite an existing `.env`.

### 1. Create the PostgreSQL role and databases

Open `psql` as a local PostgreSQL administrator:

```sh
psql -h 127.0.0.1 -U postgres -d postgres -W
```

On Windows, use SQL Shell (psql), or the installed PostgreSQL command-line tools if `psql` is not on PATH. Enter the administrator password locally. Run the following once, only if these resources do not already exist:

```sql
CREATE ROLE pseudo_app LOGIN;
\password pseudo_app
CREATE DATABASE pseudo OWNER pseudo_app;
CREATE DATABASE pseudo_test OWNER pseudo_app;
\q
```

The password prompt keeps the application password out of SQL statements. `pseudo_test` is a separate integration-test database; do not point tests at production data.

### 2. Install backend dependencies

Windows PowerShell:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

macOS / Linux:

```sh
cd backend
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `backend/.env` locally. Only placeholders are shown here:

```dotenv
DATABASE_URL=postgresql+psycopg://pseudo_app:YOUR_URL_ENCODED_PASSWORD@127.0.0.1:5432/pseudo
TEST_DATABASE_URL=postgresql+psycopg://pseudo_app:YOUR_URL_ENCODED_PASSWORD@127.0.0.1:5432/pseudo_test
COOKIE_SECURE=false
TRUSTED_ORIGINS=http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:8000

# Optional until you use Connect GitHub
GITHUB_CLIENT_ID=YOUR_GITHUB_CLIENT_ID
GITHUB_CLIENT_SECRET=YOUR_GITHUB_CLIENT_SECRET
GITHUB_CALLBACK_URL=http://127.0.0.1:8000/github/callback
FRONTEND_URL=http://127.0.0.1:5173
GITHUB_TOKEN_KEY=YOUR_GENERATED_FERNET_KEY
```

Percent-encode special characters in database URL passwords. The ordinary project/session/profile features work without GitHub configuration; leave the GitHub credentials empty until configured. Real process environment variables override `.env`.

### 3. Apply migrations and start FastAPI

From `backend`, in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn main:app --reload --host 127.0.0.1 --port 8000 --no-access-log
```

On macOS/Linux, replace `.\.venv\Scripts\python.exe` with `.venv/bin/python`.

Startup checks the migration revision. Run migrations before starting the backend; do not replace them with ad hoc table creation. Existing early-stage unowned records, if present, are archived by the account migration rather than assigned to a new user. Automatic destructive downgrades are intentionally disabled.

### 4. Start React

In a second terminal, from the project root:

```sh
cd frontend
npm ci
npm run dev
```

Open [Pseudo locally](http://127.0.0.1:5173). Register an account, create a project, and record your first session. Use `127.0.0.1` consistently for the local OAuth flow. API documentation is available at [FastAPI docs](http://127.0.0.1:8000/docs).

### 5. Optional GitHub OAuth setup

Create a GitHub **OAuth App** in [Developer settings](https://github.com/settings/developers):

| Setting | Local development value |
| --- | --- |
| Application name | Pseudo Local Development |
| Homepage URL | `http://127.0.0.1:5173` |
| Authorization callback URL | `http://127.0.0.1:8000/github/callback` |

Leave Device Flow disabled. Save the Client ID and generated Client Secret only in `backend/.env`. Generate an encryption key locally from the backend virtual environment:

```powershell
.\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Copy the result into `GITHUB_TOKEN_KEY`; do not share it. Preserve the key across restarts so stored GitHub credentials remain decryptable. Restart FastAPI after configuration changes.

In Pseudo, select **Connect GitHub**, authorize the app, and return to the dashboard. No additional OAuth scopes are requested: this MVP supports public information only. Unexpectedly broad grants are rejected. Tokens and PKCE verifiers are encrypted server-side, while OAuth state is single-use and bound to the current Pseudo login.

## API overview

| Area | Routes |
| --- | --- |
| Accounts | `POST /auth/register`, `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` |
| Projects | `GET /projects`, `POST /projects` |
| Sessions | `POST /sessions`, `GET /sessions`, `GET /sessions/active`, `POST /sessions/{id}/finish` |
| Publication | `POST /sessions/{id}/publish`, `POST /sessions/{id}/unpublish`, `GET /profiles/{pseudonym}` |
| GitHub connection | `POST /github/connect`, `GET /github/callback`, `GET /github/status`, `POST /github/disconnect` |
| Repositories | `GET /github/repos`, `GET /projects/{id}/github`, `POST /projects/{id}/github`, `GET /projects/{id}/github/activity` |
| Evidence | `GET /sessions/{id}/github-evidence`, `POST /sessions/{id}/github-evidence`, `DELETE /sessions/{id}/github-evidence/{evidence_id}` |
| Health | `GET /health` |

Private routes require an account cookie. State-changing requests also require `X-Pseudo-Request: 1`; browser origins are checked against `TRUSTED_ORIGINS`. See the generated FastAPI documentation for request schemas.

## Testing

Configure `TEST_DATABASE_URL`, then run from `backend`:

```powershell
.\.venv\Scripts\python.exe -m unittest -v
```

Integration tests create and remove isolated schemas in the test database. They exercise account isolation, cookie security, migrations, session timing, real backend restarts, publication rules, and GitHub evidence privacy. GitHub HTTP responses are mocked; automated tests do not depend on a real GitHub account. Database tests are skipped if `TEST_DATABASE_URL` is absent.

From `frontend`:

```sh
npm run build
```

The Stage 6 verification run passed all 12 backend tests and the frontend production build. Real OAuth, repository linking, commit attachment, public evidence display, and restart persistence were also checked manually. Non-empty real PR retrieval and attachment were checked in a database transaction that was rolled back.

## Privacy and project status

**Status: working local MVP through Stage 6.** This is a learning project, not a hardened production deployment.

- Passwords are hashed with Argon2id. Account sessions use HTTP-only cookies; authentication tokens are not stored in localStorage.
- Public profiles exclude email and credentials. A pseudonym is not an anonymity guarantee: publishing GitHub links or identifying text can reveal a GitHub identity.
- All sessions start private. Evidence appears publicly only with a published session and confirmed public repository visibility. GitHub failures or expired credentials can temporarily hide evidence.
- Disconnect removes the local GitHub connection, repository links, and attached evidence, while preserving projects and work sessions. Remote revocation is attempted; the UI explains how to revoke manually if it cannot be confirmed.
- Activity retrieval is limited to the latest 20 default-branch commits and 20 recently updated PRs. Repository lists paginate; a session supports up to 20 evidence items. Public responses inspect up to 20 repositories across the newest 200 evidence records.
- Private repositories, automatic synchronization, automatic posting, scoring, social features, and AI are not implemented.
- Email verification, password recovery, login rate limiting, refresh-token rotation, and production deployment configuration are not implemented. Expired GitHub authorization requires reconnecting.
- `COOKIE_SECURE=false` is for local HTTP only. Hosting requires HTTPS, secure cookies, appropriate trusted origins, protected secrets, and callback-log redaction.

## Screenshots

Screenshots from local verification contain account-specific data and are intentionally not included. The local setup above can be used for a live demonstration.
