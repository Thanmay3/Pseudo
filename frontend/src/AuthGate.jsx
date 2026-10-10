import React, { useEffect, useState } from 'react';
import { post, request } from './api.js';

export default function AuthGate({ children }) {
  const [user, setUser] = useState(null);
  const [checking, setChecking] = useState(true);
  const [mode, setMode] = useState('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [pseudonym, setPseudonym] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function checkAccount() {
    setChecking(true);
    setError('');
    try { setUser(await request('/auth/me')); }
    catch (err) { if (err.status !== 401) setError('Cannot reach the backend. Check the server and retry.'); }
    finally { setChecking(false); }
  }
  useEffect(() => { checkAccount(); }, []);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      const data = { email, password, ...(mode === 'register' ? { pseudonym } : {}) };
      setUser(await post(`/auth/${mode}`, data));
      setPassword('');
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }

  async function logout() {
    setBusy(true);
    setError('');
    try { await post('/auth/logout', {}); setUser(null); setPassword(''); }
    catch { setError('Logout failed. Please retry; your session may still be active.'); }
    finally { setBusy(false); }
  }

  if (checking) return <div className="auth-shell"><p role="status">Checking your account…</p></div>;
  if (user) return <>{error && <p className="error" role="alert">{error}</p>}{children({ user, logout, busy, expire: () => { setUser(null); setError('Your session expired. Please log in again.'); } })}</>;
  return <div className="auth-shell">
    <a className="brand" href="#"><span className="brand-mark">p.</span>Pseudo</a>
    <h1>Build in public.<br /><span>Stay private.</span></h1>
    <section className="work-panel"><h2>{mode === 'register' ? 'Create your account' : 'Welcome back'}</h2>
      <p className="stat-note">Your pseudonym represents you. Your email stays private.</p>
      {error && <div><p className="error" role="alert">{error}</p><button className="secondary" onClick={checkAccount} disabled={busy}>Retry connection</button></div>}
      <form onSubmit={submit}>
        <label htmlFor="auth-email">Email</label><input id="auth-email" type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} maxLength={254} required disabled={busy} />
        <label htmlFor="auth-password">Password</label><input id="auth-password" type="password" autoComplete={mode === 'register' ? 'new-password' : 'current-password'} value={password} onChange={(e) => setPassword(e.target.value)} minLength={mode === 'register' ? 12 : 1} maxLength={128} required disabled={busy} />
        {mode === 'register' && <><p className="stat-note">Use 12–128 characters. Passwords are never stored in plain text.</p><label htmlFor="auth-pseudonym">Choose pseudonym</label><input id="auth-pseudonym" autoComplete="nickname" value={pseudonym} onChange={(e) => setPseudonym(e.target.value)} minLength={3} maxLength={30} pattern="[A-Za-z0-9_]+" required disabled={busy} /><p className="stat-note">3–30 letters, numbers, or underscores. Unique regardless of capitalization.</p></>}
        <button className="primary" disabled={busy}>{busy ? 'Please wait…' : mode === 'register' ? 'Create Account' : 'Login'}</button>
      </form>
      <button className="auth-switch" disabled={busy} onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setPassword(''); setError(''); }}>{mode === 'login' ? 'New here? Register' : 'Already have an account? Login'}</button>
    </section>
  </div>;
}
