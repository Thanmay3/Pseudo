"""Run with python -m unittest -v. Integration test requires TEST_DATABASE_URL.

Only a uniquely named test schema is created/dropped; existing tables are untouched.
The integration test launches and restarts real Uvicorn processes.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen, build_opener, HTTPCookieProcessor
from http.cookiejar import CookieJar
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from main import ProjectInput, StartInput
from auth import hasher, secure_cookie


class ValidationTests(unittest.TestCase):
    def test_https_cookie_setting(self):
        with patch.dict(os.environ, {'COOKIE_SECURE': 'true'}):
            self.assertTrue(secure_cookie())
        with patch.dict(os.environ, {'COOKIE_SECURE': 'false'}):
            self.assertFalse(secure_cookie())

    def test_required_fields_and_server_owned_time(self):
        self.assertEqual(ProjectInput(name=' Pseudo ').name, 'Pseudo')
        for payload in ({}, {'name': '   '}):
            with self.assertRaises(ValidationError):
                ProjectInput(**payload)
        for payload in (
            {'project_id': 'x', 'task': '   '},
            {'project_id': 'x', 'task': 'Work', 'duration_seconds': 999},
        ):
            with self.assertRaises(ValidationError):
                StartInput(**payload)


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'Set TEST_DATABASE_URL to run real PostgreSQL persistence tests')
class PersistenceTests(unittest.TestCase):
    def setUp(self):
        url = make_url(os.environ['TEST_DATABASE_URL'])
        if url.get_backend_name() != 'postgresql':
            self.fail('Tests require PostgreSQL; no database substitution is allowed')
        url = url.set(drivername='postgresql+psycopg')
        self.engine = create_engine(url, hide_parameters=True)
        self.schema = 'pseudo_test_' + uuid4().hex
        with self.engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.addCleanup(self.clean_schema)
        self.environment = os.environ.copy()
        self.environment['DATABASE_URL'] = url.update_query_dict(
            {'options': f'-csearch_path={self.schema}'}
        ).render_as_string(hide_password=False)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        # Simulate Stage 3 tables so every integration test verifies preservation.
        with self.engine.begin() as connection:
            connection.execute(text(f'CREATE TABLE "{self.schema}".projects (id varchar(36) PRIMARY KEY, name text NOT NULL)'))
            connection.execute(text(f'CREATE TABLE "{self.schema}".build_sessions (id varchar(36) PRIMARY KEY, project_id varchar(36) REFERENCES "{self.schema}".projects(id), end_time timestamptz)'))
            connection.execute(text(f"INSERT INTO \"{self.schema}\".projects VALUES ('legacy-project', 'Unowned demo')".replace('\"', '"')))
            connection.execute(text(f"INSERT INTO \"{self.schema}\".build_sessions VALUES ('legacy-session', 'legacy-project', NULL)".replace('\"', '"')))
        result = subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'],
            cwd=Path(__file__).parent, env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.cookies = CookieJar()
        self.client = build_opener(HTTPCookieProcessor(self.cookies))
        self.last_headers = None
        self.process = None
        self.addCleanup(self.stop_backend)
        self.start_backend()

    def clean_schema(self):
        with self.engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.engine.dispose()

    def start_backend(self):
        self.process = subprocess.Popen(
            [sys.executable, '-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', str(self.port)],
            cwd=Path(__file__).parent, env=self.environment,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            if self.process.poll() is not None:
                self.fail('Test backend failed to start; check PostgreSQL configuration')
            try:
                if self.api('/health')[0] == 200:
                    return
            except (URLError, OSError):
                pass
            time.sleep(0.1)
        self.fail('Test backend did not become ready')

    def stop_backend(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)

    def restart(self):
        self.stop_backend()
        self.start_backend()

    def api(self, path, body=None, headers=None):
        request = Request(
            f'http://127.0.0.1:{self.port}{path}',
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type': 'application/json', 'X-Pseudo-Request': '1', **(headers or {})},
        )
        try:
            response = self.client.open(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            self.last_headers = response.headers
            return response.status, json.load(response)

    def register(self, letter='a'):
        data = {'email': f'{letter}@example.com', 'password': 'test-only password 123!', 'pseudonym': f'Builder_{letter}'}
        code, user = self.api('/auth/register', data)
        self.assertEqual(code, 201)
        return data, user

    def test_full_flow_and_actual_process_restarts(self):
        self.register()
        self.assertEqual(self.api('/projects', {})[0], 422)
        self.assertEqual(self.api('/sessions', {'project_id': 'missing', 'task': 'Work'})[0], 404)
        self.assertEqual(self.api('/sessions/missing/finish', {})[0], 404)
        code, project = self.api('/projects', {'name': 'Persistence check', 'description': 'Integration test'})
        self.assertEqual(code, 201)
        self.assertIn('created_at', project)
        payload = {'project_id': project['id'], 'task': 'Verify persistence'}
        self.assertEqual(self.api('/sessions', {**payload, 'duration_seconds': 99})[0], 422)
        code, running = self.api('/sessions', payload)
        self.assertEqual(code, 201)
        self.assertIsNone(running['end_time'])
        self.assertEqual(self.api('/sessions', payload)[0], 409)
        self.assertEqual(self.api('/sessions')[1]['total_sessions'], 0)
        self.restart()
        self.assertEqual(self.api('/sessions/active')[1]['session'], running)
        time.sleep(1.1)
        code, finished = self.api(f"/sessions/{running['id']}/finish", {'accomplishment': 'Verified'})
        self.assertEqual(code, 200)
        self.assertGreaterEqual(finished['duration_seconds'], 1)
        self.assertEqual(self.api(f"/sessions/{running['id']}/finish", {'accomplishment': 'Overwrite'})[1], finished)
        before = self.api('/sessions')[1]
        self.assertEqual(before['total_sessions'], 1)
        self.assertEqual(before['total_duration_seconds'], finished['duration_seconds'])
        self.restart()
        self.assertEqual(self.api('/projects')[1], [project])
        self.assertEqual(self.api('/sessions')[1], before)
        self.assertIsNone(self.api('/sessions/active')[1]['session'])
        _, second = self.api('/sessions', payload)
        self.api(f"/sessions/{second['id']}/finish", {})
        log = self.api('/sessions')[1]
        self.assertEqual(log['total_sessions'], 2)
        self.assertEqual(log['sessions'][0]['id'], second['id'])
        self.assertEqual(log['total_duration_seconds'], sum(s['duration_seconds'] for s in log['sessions']))

    def test_accounts_isolation_cookies_and_archive(self):
        for path in ('/projects', '/sessions', '/sessions/active', '/auth/me'):
            self.assertEqual(self.api(path)[0], 401)
        self.assertEqual(self.api('/projects', {'name': 'No account'})[0], 401)
        self.assertEqual(self.api('/sessions', {'project_id': 'x', 'task': 'No account'})[0], 401)
        self.assertEqual(self.api('/sessions/x/finish', {})[0], 401)
        secret = 'short'
        code, invalid = self.api('/auth/register', {'email': 'bad', 'password': secret, 'pseudonym': '!bad'})
        self.assertEqual(code, 422)
        self.assertNotIn(secret, json.dumps(invalid))
        self.assertEqual(self.api('/auth/register', {})[0], 422)
        credentials_a, user_a = self.register()
        cookie = self.last_headers.get('Set-Cookie')
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=lax', cookie)
        self.assertIn('Max-Age=604800', cookie)
        self.assertNotIn('password_hash', user_a)
        self.assertEqual(self.api('/auth/me')[1], user_a)
        with self.engine.connect() as db:
            stored = db.execute(text(f'SELECT password_hash FROM "{self.schema}".users WHERE id=:id'), {'id': user_a['id']}).scalar_one()
            self.assertTrue(stored.startswith('$argon2id$'))
            self.assertTrue(hasher.verify(stored, credentials_a['password']))
            token_digest = db.execute(text(f'SELECT token_hash FROM "{self.schema}".auth_sessions')).scalar_one()
            self.assertNotEqual(token_digest, next(iter(self.cookies)).value)
            self.assertEqual(db.execute(text(f'SELECT name FROM "{self.schema}".stage3_projects')).scalar_one(), 'Unowned demo')
            self.assertEqual(db.execute(text(f'SELECT project_id FROM "{self.schema}".stage3_build_sessions')).scalar_one(), 'legacy-project')
        self.assertEqual(self.api('/projects')[1], [])
        self.assertEqual(self.api('/sessions', {'project_id': 'legacy-project', 'task': 'Claim archive'})[0], 404)
        self.assertEqual(self.api('/auth/register', {**credentials_a, 'email': 'A@EXAMPLE.COM', 'pseudonym': 'another'})[0], 409)
        self.assertEqual(self.api('/auth/register', {**credentials_a, 'email': 'different@example.com', 'pseudonym': 'builder_A'})[0], 409)
        self.assertEqual(self.api('/projects', {'name': 'CSRF'}, {'X-Pseudo-Request': ''})[0], 403)
        self.assertEqual(self.api('/projects', {'name': 'CSRF'}, {'Origin': 'https://attacker.example'})[0], 403)
        old_token = next(iter(self.cookies)).value
        self.assertEqual(self.api('/auth/logout', {})[0], 200)
        self.assertEqual(self.api('/auth/me', headers={'Cookie': f'pseudo_session={old_token}'})[0], 401)
        self.assertEqual(self.api('/auth/login', {'email': credentials_a['email'], 'password': 'wrong password'})[0], 401)
        self.assertEqual(self.api('/auth/login', {'email': credentials_a['email'], 'password': credentials_a['password']})[0], 200)
        _, project = self.api('/projects', {'name': 'Private A'})
        _, active = self.api('/sessions', {'project_id': project['id'], 'task': 'Build A'})
        _, finished = self.api(f"/sessions/{active['id']}/finish", {'accomplishment': 'Done A'})
        self.api('/auth/logout', {})
        credentials_b, _ = self.register('b')
        self.assertEqual(self.api('/projects')[1], [])
        self.assertEqual(self.api('/sessions')[1]['total_sessions'], 0)
        self.assertIsNone(self.api('/sessions/active')[1]['session'])
        self.assertEqual(self.api('/sessions', {'project_id': project['id'], 'task': 'Steal A'})[0], 404)
        self.assertEqual(self.api(f"/sessions/{active['id']}/finish", {'accomplishment': 'Overwrite A'})[0], 404)
        self.api('/auth/logout', {})
        self.assertEqual(self.api('/auth/login', {'email': credentials_b['email'], 'password': credentials_b['password']})[0], 200)
        self.assertEqual(self.api('/projects')[1], [])
        self.api('/auth/logout', {})
        self.assertEqual(self.api('/auth/login', {'email': credentials_a['email'], 'password': credentials_a['password']})[0], 200)
        self.assertEqual(self.api('/projects')[1], [project])
        self.assertEqual(self.api('/sessions')[1]['sessions'], [finished])
        with self.engine.begin() as db:
            db.execute(text(f"UPDATE \"{self.schema}\".auth_sessions SET expires_at=now()-interval '1 second'"))
        self.assertEqual(self.api('/auth/me')[0], 401)

    def test_concurrent_starts_are_per_user(self):
        self.register()
        _, project = self.api('/projects', {'name': 'Concurrent'})
        with ThreadPoolExecutor(max_workers=2) as workers:
            codes = list(workers.map(lambda _: self.api('/sessions', {'project_id': project['id'], 'task': 'Work'})[0], range(2)))
        self.assertEqual(sorted(codes), [201, 409])
        self.api('/auth/logout', {})
        self.register('b')
        _, second_project = self.api('/projects', {'name': 'B can work too'})
        self.assertEqual(self.api('/sessions', {'project_id': second_project['id'], 'task': 'Work B'})[0], 201)

    def test_https_cookie_header(self):
        self.environment['COOKIE_SECURE'] = 'true'
        self.restart()
        self.register()
        self.assertIn('Secure', self.last_headers.get('Set-Cookie'))
        self.assertIn('HttpOnly', self.last_headers.get('Set-Cookie'))

    def test_public_profiles_visibility_and_ownership(self):
        anonymous = build_opener()

        def public(pseudonym):
            try:
                response = anonymous.open(f'http://127.0.0.1:{self.port}/profiles/{pseudonym}', timeout=10)
            except HTTPError as error:
                response = error
            with response:
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                return response.status, json.load(response)

        self.assertEqual(public('not_a_user')[0], 404)
        credentials, user = self.register()
        name = user['pseudonym']
        _, empty = public(name.lower())
        self.assertEqual(empty, {'pseudonym': name, 'total_projects': 0, 'total_sessions': 0, 'total_duration_seconds': 0, 'sessions': []})
        _, project = self.api('/projects', {'name': 'Public project', 'description': 'PRIVATE PROJECT DESCRIPTION'})
        _, private_project = self.api('/projects', {'name': 'SECRET PROJECT', 'description': 'SECRET DETAILS'})
        self.assertEqual(self.api('/sessions', {'project_id': project['id'], 'task': 'Bypass', 'is_published': True})[0], 422)

        def finish(project_id, task):
            code, session = self.api('/sessions', {'project_id': project_id, 'task': task})
            self.assertEqual(code, 201)
            self.assertFalse(session['is_published'])
            self.assertEqual(self.api(f"/sessions/{session['id']}/publish", {})[0], 409)
            self.assertEqual(public(name)[1]['sessions'], [])
            code, completed = self.api(f"/sessions/{session['id']}/finish", {'accomplishment': task + ' accomplished'})
            self.assertEqual(code, 200)
            self.assertFalse(completed['is_published'])
            return completed

        first = finish(project['id'], 'Published work one')
        second = finish(project['id'], 'Published work two')
        hidden = finish(private_project['id'], 'SECRET TASK')
        self.assertEqual(public(name)[1], empty)
        # Deterministic backend fixtures verify sums without waiting hours.
        with self.engine.begin() as db:
            for session, seconds in ((first, 3600), (second, 1800), (hidden, 9000)):
                db.execute(text(f'UPDATE "{self.schema}".build_sessions SET start_time=end_time-make_interval(secs => :seconds), duration_seconds=:seconds WHERE id=:id'), {'seconds': seconds, 'id': session['id']})
        for session in (first, second):
            code, published = self.api(f"/sessions/{session['id']}/publish", {})
            self.assertEqual(code, 200)
            self.assertTrue(published['is_published'])
            self.assertEqual(self.api(f"/sessions/{session['id']}/publish", {})[0], 200)
        code, profile = public(name)
        self.assertEqual(code, 200)
        self.assertEqual(profile['total_projects'], 1)
        self.assertEqual(profile['total_sessions'], 2)
        self.assertEqual(profile['total_duration_seconds'], 5400)
        self.assertEqual([s['task'] for s in profile['sessions']], ['Published work two', 'Published work one'])
        self.assertEqual(set(profile), {'pseudonym', 'total_projects', 'total_sessions', 'total_duration_seconds', 'sessions'})
        for item in profile['sessions']:
            self.assertEqual(set(item), {'project_name', 'task', 'accomplishment', 'end_time', 'duration_seconds', 'evidence'})
        for secret in (credentials['email'], credentials['password'], user['id'], 'SECRET', 'PRIVATE PROJECT DESCRIPTION', 'password_hash', 'token'):
            self.assertNotIn(secret, json.dumps(profile))
        self.assertEqual(self.api('/sessions')[1]['total_duration_seconds'], 14400)
        self.api(f"/sessions/{hidden['id']}/publish", {})
        self.assertEqual(public(name)[1]['total_projects'], 2)
        self.assertEqual(public(name)[1]['total_duration_seconds'], 14400)
        self.api(f"/sessions/{hidden['id']}/unpublish", {})
        self.restart()
        self.assertEqual(public(name)[1], profile)
        self.api('/auth/logout', {})
        for action in ('publish', 'unpublish'):
            self.assertEqual(self.api(f"/sessions/{first['id']}/{action}", {})[0], 401)
        self.register('b')
        for action in ('publish', 'unpublish'):
            self.assertEqual(self.api(f"/sessions/{first['id']}/{action}", {})[0], 404)
        self.assertEqual(public(name)[1], profile)
        self.api('/auth/logout', {})
        self.api('/auth/login', {'email': credentials['email'], 'password': credentials['password']})
        self.assertEqual(self.api(f"/sessions/{first['id']}/unpublish", {})[0], 200)
        remaining = public(name)[1]
        self.assertEqual(remaining['total_duration_seconds'], 1800)
        self.assertEqual(remaining['total_sessions'], 1)
        self.assertEqual(remaining['total_projects'], 1)
        self.api(f"/sessions/{second['id']}/unpublish", {})
        self.assertEqual(public(name)[1], empty)
        self.restart()
        self.assertEqual(public(name)[1], empty)
        self.assertEqual(self.api('/sessions')[1]['total_sessions'], 3)

    def test_stage4_upgrade_keeps_existing_sessions_private(self):
        # A separate isolated schema simulates an existing Stage 4 database.
        schema = 'upgrade_' + uuid4().hex
        with self.engine.begin() as db:
            db.execute(text(f'CREATE SCHEMA "{schema}"'))
        env = self.environment.copy()
        env['DATABASE_URL'] = make_url(env['DATABASE_URL']).update_query_dict({'options': f'-csearch_path={schema}'}).render_as_string(hide_password=False)
        try:
            result = subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', '0001_accounts'], cwd=Path(__file__).parent, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            with self.engine.begin() as db:
                db.execute(text(f"INSERT INTO \"{schema}\".users VALUES ('old-user', 'old@example.com', 'unusable-test-hash', 'OldUser', now())"))
                db.execute(text(f"INSERT INTO \"{schema}\".projects VALUES ('old-project', 'old-user', 'Old project', '', now())"))
                db.execute(text(f"INSERT INTO \"{schema}\".build_sessions VALUES ('old-session', 'old-project', 'Old task', now()-interval '1 hour', now(), 3600, 'Old accomplishment', now())"))
            result = subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], cwd=Path(__file__).parent, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            with self.engine.connect() as db:
                row = db.execute(text(f'SELECT task, duration_seconds, is_published FROM "{schema}".build_sessions')).one()
                self.assertEqual(tuple(row), ('Old task', 3600, False))
        finally:
            with self.engine.begin() as db:
                db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


if __name__ == '__main__':
    unittest.main()
