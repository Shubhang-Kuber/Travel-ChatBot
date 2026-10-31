import { useEffect, useRef, useState, type FormEvent } from 'react';
import { LogOut, PanelLeftClose, PanelLeftOpen, Plus, Send } from 'lucide-react';
import type { ChatMessage } from '../hooks/useChat';
import { Logo } from './Brand';
import { Message } from './Message';

const SUGGESTIONS = [
  '3 days in Lisbon on a budget',
  'Best street food in Hanoi',
  'Is October a good time for Kyoto?',
];

interface Props {
  messages: ChatMessage[];
  busy: boolean;
  step: number;
  email: string;
  planOpen: boolean;
  onTogglePlan: () => void;
  onSend: (text: string) => void;
  onNewTrip: () => void;
  onSignOut: () => void;
}

export function Chat({ messages, busy, step, email, planOpen, onTogglePlan, onSend, onNewTrip, onSignOut }: Props) {
  const [draft, setDraft] = useState('');
  const logRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);

  // Follow the answer as it types, unless the reader has scrolled up to re-read something.
  useEffect(() => {
    const log = logRef.current;
    if (log && stickRef.current) log.scrollTop = log.scrollHeight;
  }, [messages, step]);

  const onScroll = () => {
    const log = logRef.current;
    if (log) stickRef.current = log.scrollHeight - log.scrollTop - log.clientHeight < 140;
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const text = draft.trim();
    if (!text || busy) return;
    setDraft('');
    stickRef.current = true;
    onSend(text);
  };

  return (
    <main className="chat">
      <header className="chat-head">
        <button
          type="button"
          className="icon-btn"
          onClick={onTogglePlan}
          aria-label={planOpen ? 'Hide trip planner' : 'Show trip planner'}
          aria-expanded={planOpen}
        >
          {planOpen ? <PanelLeftClose size={20} /> : <PanelLeftOpen size={20} />}
        </button>
        <Logo />
        <span className="mono online">Online</span>
        <span className="spacer" />
        <span className="who" title={email}>
          {email}
        </span>
        <button type="button" className="btn btn-ghost" onClick={onNewTrip} disabled={busy} title="Start a new conversation">
          <Plus size={18} /> <span className="btn-label">New trip</span>
        </button>
        <button type="button" className="icon-btn" onClick={onSignOut} aria-label="Sign out">
          <LogOut size={20} />
        </button>
      </header>

      <div className="log" ref={logRef} onScroll={onScroll} aria-live="polite">
        {messages.length === 0 ? (
          <div className="empty">
            <span className="mono">Travel assistant</span>
            <h2>
              Ready when <em>you are.</em>
            </h2>
            <p>Plan a route in the panel, or ask anything about where you are headed.</p>
            <div className="suggestions">
              {SUGGESTIONS.map(text => (
                <button key={text} type="button" className="suggestion" onClick={() => onSend(text)} disabled={busy}>
                  {text}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="log-inner">
            {messages.map(message => (
              <Message key={message.id} message={message} step={step} />
            ))}
          </div>
        )}
      </div>

      <form className="composer" onSubmit={submit} autoComplete="off">
        <div className="composer-inner">
          <label className="sr-only" htmlFor="chat-input">
            Message
          </label>
          <input
            id="chat-input"
            className="composer-input"
            value={draft}
            onChange={e => setDraft(e.target.value)}
            maxLength={4000}
            placeholder="Ask anything, e.g. what about food?"
            disabled={busy}
          />
          <button type="submit" className="send" disabled={busy || !draft.trim()} aria-label="Send message">
            <Send size={18} />
          </button>
        </div>
      </form>
    </main>
  );
}
