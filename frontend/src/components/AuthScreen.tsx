import { useState, type FormEvent } from 'react';
import { motion } from 'framer-motion';
import { ArrowRight, Loader2 } from 'lucide-react';
import { ApiError, describeError, logIn, signUp } from '../api/client';
import { Logo } from './Brand';

type Mode = 'login' | 'signup';

function authMessage(err: unknown, mode: Mode): string {
  if (err instanceof ApiError) {
    if (err.code === 'email_taken') return 'That email is already registered. Sign in instead.';
    if (err.code === 'invalid_credentials') return 'Wrong email or password.';
    if (err.code === 'validation_error' && mode === 'signup') {
      return 'Use a valid email and a password of 8 to 72 characters.';
    }
  }
  return describeError(err);
}

/** Decorative route: three stops joined by a dashed arc, with a plane that keeps flying along it. */
function RouteArc() {
  return (
    <svg className="arc" viewBox="0 0 560 170" role="img" aria-label="A flight path across three cities">
      <path className="arc-path" d="M 20 120 C 140 -10, 300 -10, 540 100" />
      <circle className="arc-dot" cx="20" cy="120" r="7" />
      <circle className="arc-dot" cx="235" cy="20" r="7" />
      <circle className="arc-dot" cx="540" cy="100" r="7" />
      <text className="arc-label" x="20" y="150" textAnchor="start">
        LISBON
      </text>
      <text className="arc-label" x="235" y="52" textAnchor="middle">
        ROME
      </text>
      <text className="arc-label" x="540" y="130" textAnchor="end">
        ISTANBUL
      </text>
      <g className="arc-plane">
        <path d="M11 0-9-9l5 9-5 9z" fill="currentColor" />
      </g>
    </svg>
  );
}

export function AuthScreen({ notice, onSignedIn }: { notice: string | null; onSignedIn: () => void }) {
  const [mode, setMode] = useState<Mode>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const switchMode = (next: Mode) => {
    setMode(next);
    setError(null);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      if (mode === 'signup') await signUp(email, password);
      await logIn(email, password);
      onSignedIn();
    } catch (err) {
      setError(authMessage(err, mode));
    } finally {
      setSubmitting(false);
    }
  };

  const message = error ?? notice;

  return (
    <div className="welcome">
      <motion.section
        className="hero"
        initial={{ opacity: 0, y: 16 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.6, ease: 'easeOut' }}
      >
        <Logo />
        <h1>
          Where to <em>next?</em>
        </h1>
        <p>Tell TravelBot your stops and dates. It builds the itinerary and answers your questions along the way.</p>
        <RouteArc />
      </motion.section>

      <motion.section
        className="pass"
        initial={{ opacity: 0, y: 24 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.6, delay: 0.1, ease: 'easeOut' }}
        aria-labelledby="auth-title"
      >
        <div className="pass-body">
          <div>
            <h2 id="auth-title">{mode === 'login' ? 'Welcome back' : 'Create your account'}</h2>
            <p className="pass-sub">
              {mode === 'login' ? 'Sign in to pick up your trip.' : 'One account keeps all your trips together.'}
            </p>
          </div>

          <div className="tabs" role="tablist" aria-label="Account">
            <button
              type="button"
              role="tab"
              className="tab"
              aria-selected={mode === 'login'}
              onClick={() => switchMode('login')}
            >
              Sign in
            </button>
            <button
              type="button"
              role="tab"
              className="tab"
              aria-selected={mode === 'signup'}
              onClick={() => switchMode('signup')}
            >
              Create account
            </button>
          </div>

          <form className="auth-form" onSubmit={submit}>
            <label className="field">
              <span>Email</span>
              <input
                className="input"
                type="email"
                value={email}
                onChange={e => setEmail(e.target.value)}
                placeholder="you@example.com"
                autoComplete="email"
                required
              />
            </label>
            <label className="field">
              <span>Password</span>
              <input
                className="input"
                type="password"
                value={password}
                onChange={e => setPassword(e.target.value)}
                placeholder="At least 8 characters"
                autoComplete={mode === 'signup' ? 'new-password' : 'current-password'}
                minLength={mode === 'signup' ? 8 : undefined}
                required
              />
            </label>
            {message && (
              <p className="form-error" role="alert">
                {message}
              </p>
            )}
            <button type="submit" className="btn btn-primary btn-block" disabled={submitting}>
              {submitting ? <Loader2 size={18} className="spin" /> : <ArrowRight size={18} />}
              {mode === 'login' ? 'Sign in' : 'Create account'}
            </button>
          </form>
        </div>
      </motion.section>
    </div>
  );
}
