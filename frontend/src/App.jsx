import React, { useEffect, useState } from 'react';
import { post, request } from './api.js';
import AuthGate from './AuthGate.jsx';
import PublicProfile from './PublicProfile.jsx';
import GitHubPanel from './GitHubPanel.jsx';

function duration(seconds) {
  const value = Math.max(0, Math.floor(seconds));
  return `${Math.floor(value / 3600)}h ${Math.floor(value % 3600 / 60)}m ${value % 60}s`;
}

export default function App() {
  const match = window.location.pathname.match(/^\/u\/([^/]+)\/?$/);
  if (match) {
    let pseudonym;
    try { pseudonym = decodeURIComponent(match[1]); } catch { pseudonym = ''; }
    return <PublicProfile pseudonym={pseudonym} />;
  }
  return <AuthGate>{({ user, logout, busy, expire }) => <Dashboard key={user.id} user={user} logout={logout} authBusy={busy} expire={expire} />}</AuthGate>;
}

function Dashboard({ user, logout, authBusy, expire }) {
  const [projects, setProjects] = useState([]);
  const [log, setLog] = useState(null);
  const [active, setActive] = useState(null);
  const [clock, setClock] = useState({ elapsed: 0, sampledAt: performance.now() });
  const [elapsed, setElapsed] = useState(0);
  const [selected, setSelected] = useState('');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [task, setTask] = useState('');
  const [accomplishment, setAccomplishment] = useState('');
  const [connection, setConnection] = useState('checking');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function loadDashboard() {
    try {
      const [health, nextProjects, nextLog, running] = await Promise.all([
        request('/health'), request('/projects'), request('/sessions'), request('/sessions/active'),
      ]);
      if (health.status !== 'ok') throw new Error('Unexpected health response');
      setProjects(nextProjects);
      setLog(nextLog);
      setActive(running.session);
      const seconds = running.session ? (Date.parse(running.server_time) - Date.parse(running.session.start_time)) / 1000 : 0;
      setClock({ elapsed: seconds, sampledAt: performance.now() });
      setElapsed(seconds);
      setSelected((current) => running.session?.project_id || (nextProjects.some((p) => p.id === current) ? current : nextProjects[0]?.id || ''));
      setConnection('connected');
    } catch (err) {
      if (err.status === 401) expire();
      setConnection('offline');
      throw err;
    }
  }

  useEffect(() => {
    loadDashboard().catch(() => setError('Could not load the dashboard. Check that FastAPI is running, then refresh.'));
  }, []);

  useEffect(() => {
    if (!active) return;
    const tick = () => setElapsed(clock.elapsed + (performance.now() - clock.sampledAt) / 1000);
    tick();
    const interval = setInterval(tick, 1000);
    return () => clearInterval(interval);
  }, [active, clock]);

  async function perform(action) {
    setBusy(true);
    setError('');
    try {
      await action();
      await loadDashboard();
    } catch (err) {
      setError(`${err.message} Refresh to reconcile with the backend before retrying.`);
      // A request may have reached the server even if its response was lost.
      try { await loadDashboard(); } catch { /* Keep the last visible data. */ }
    } finally {
      setBusy(false);
    }
  }

  const currentProject = projects.find((p) => p.id === (active?.project_id || selected));
  const ready = connection === 'connected' && !busy;

  return <div className="app-shell">
    <header className="header"><a className="brand" href="#"><span className="brand-mark">p.</span>Pseudo<span className="version">EARLY BUILD</span></a><div className="account-menu"><strong>{user.pseudonym}</strong><button className="secondary" disabled={busy || authBusy} onClick={logout}>Logout</button></div></header>
    <main>
      <section className="intro"><div><p className="eyebrow">YOUR SPACE TO MAKE THINGS</p><h1>Build in public.<br /><span>Stay private.</span></h1><p className="intro-copy">Choose a project. Put in the time. Document your progress.</p></div></section>
      <div className="section-heading"><h2>Your dashboard</h2><div className="account-menu"><a href={`/u/${encodeURIComponent(user.pseudonym)}`}>View public profile</a><button className="secondary" disabled={busy} onClick={() => perform(async () => {})}>Refresh</button></div></div>
      {error && <p className="error" role="alert">{error}</p>}
      <section className="stats" aria-label="Build statistics">
        <article className="stat-card project-card"><p className="stat-label">CURRENT PROJECT</p><h3>{currentProject?.name || 'No project yet'}</h3><p className="stat-note">{currentProject?.description || 'Create a project to get started.'}</p></article>
        <article className="stat-card"><p className="stat-label">DOCUMENTED TIME</p><p className="metric-value">{log ? duration(log.total_duration_seconds) : '—'}</p><p className="stat-note">Completed sessions only</p></article>
        <article className="stat-card"><p className="stat-label">TOTAL SESSIONS</p><p className="stat-number">{log?.total_sessions ?? '—'}</p><p className="stat-note">Each session is a step forward</p></article>
      </section>
      <div className="workspace-grid">
        <section className="work-panel"><h2>Projects</h2>
          {projects.length === 0 && <p className="empty">Create your first project below.</p>}
          <ul className="project-list">{projects.map((project) => <li key={project.id}><button className={`project-option ${selected === project.id ? 'selected' : ''}`} aria-pressed={selected === project.id} disabled={!ready || !!active} onClick={() => setSelected(project.id)}><strong>{project.name}</strong><span>{project.description || 'No description'}</span></button></li>)}</ul>
          <form onSubmit={(event) => { event.preventDefault(); perform(async () => { const project = await post('/projects', { name, description }); setSelected(project.id); setName(''); setDescription(''); }); }}>
            <label htmlFor="project-name">Project name</label><input id="project-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={100} required placeholder="e.g. Pseudo" disabled={busy} />
            <label htmlFor="project-description">Description <span className="optional">optional</span></label><textarea id="project-description" value={description} onChange={(e) => setDescription(e.target.value)} maxLength={1000} placeholder="What are you building?" disabled={busy} />
            <button className="primary" disabled={!ready || !name.trim()}>Create project</button>
          </form>
        </section>
        <section className="work-panel"><h2>{active ? 'Session in progress' : 'Start a build session'}</h2>
          {active ? <>
            <p className="active-project">{currentProject?.name}</p><h3 className="active-task">{active.task}</h3><div className="timer" role="timer" aria-label="Elapsed time">{duration(elapsed)}</div><p className="stat-note">Time is recorded by the backend. You can safely reload this page.</p>
            <form onSubmit={(event) => { event.preventDefault(); perform(async () => { await post(`/sessions/${active.id}/finish`, { accomplishment }); setAccomplishment(''); }); }}>
              <label htmlFor="accomplishment">What did you accomplish? <span className="optional">optional</span></label><textarea id="accomplishment" value={accomplishment} onChange={(e) => setAccomplishment(e.target.value)} maxLength={2000} placeholder="A small win, a useful lesson, something shipped…" disabled={busy} />
              <button className="primary" disabled={!ready}>Finish Session</button>
            </form>
          </> : <form onSubmit={(event) => { event.preventDefault(); perform(async () => { await post('/sessions', { project_id: selected, task }); setTask(''); }); }}>
            <label htmlFor="session-project">Project</label><select id="session-project" value={selected} onChange={(e) => setSelected(e.target.value)} required disabled={!ready || !projects.length}><option value="" disabled>Select a project</option>{projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select>
            <label htmlFor="session-task">Working on</label><textarea id="session-task" value={task} onChange={(e) => setTask(e.target.value)} required maxLength={500} placeholder="e.g. Building the profile page" disabled={busy} />
            <button className="primary" disabled={!ready || !selected || !task.trim()}>Start Session</button>
          </form>}
        </section>
      </div>
      <GitHubPanel key={selected || "no-project"} project={projects.find((p) => p.id === selected)} sessions={log?.sessions || []} expire={expire} />
      <section className="build-log" aria-labelledby="log-title"><div className="log-header"><div><p className="eyebrow">THE WORK ADDS UP</p><h2 id="log-title">Build Log</h2></div><span className="muted">Newest first</span></div>
        <p className="publishing-note">Sessions are private by default. Publishing shares the project name, task, accomplishment, date, and duration on your public profile. Review your text before publishing.</p>
        {!log ? <p className="empty">Waiting for the backend…</p> : log.sessions.length === 0 ? <p className="empty">Your first completed session will appear here.</p> : <ol className="session-list">{log.sessions.map((session) => <li className="session" key={session.id}><div className="session-date"><time dateTime={session.end_time}>{new Date(session.end_time).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}</time><span className="duration">{duration(session.duration_seconds)}</span></div><div className="session-content"><p className="active-project">{projects.find((p) => p.id === session.project_id)?.name || 'Unknown project'}</p><h3>{session.task}</h3>{session.accomplishment && <p>{session.accomplishment}</p>}</div><div className="publish-controls"><span className={session.is_published ? 'published-label' : 'muted'}>{session.is_published ? 'Published' : 'Private'}</span><button className="secondary" disabled={!ready} onClick={() => perform(() => post(`/sessions/${session.id}/${session.is_published ? 'unpublish' : 'publish'}`, {}))}>{session.is_published ? 'Unpublish' : 'Publish'}</button></div></li>)}</ol>}
      </section>
      <aside className="footnote"><p><strong>Saved to PostgreSQL.</strong> Projects and sessions survive backend restarts. Unsaved form text is lost on page reload.</p></aside>
    </main>
    <footer><span>Pseudo / Let the work speak.</span><span className={`connection ${connection}`} role="status"><span className="status-dot" />{connection === 'connected' ? 'Backend connected' : connection === 'checking' ? 'Connecting…' : 'Backend unavailable'}</span></footer>
  </div>;
}
