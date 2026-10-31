import { useCallback, useEffect, useState } from 'react';
import { session, setSessionExpiredHandler, signOut } from './api/client';
import { AuthScreen } from './components/AuthScreen';
import { Chat } from './components/Chat';
import { Planner } from './components/Planner';
import { useChat } from './hooks/useChat';

const NARROW = '(max-width: 900px)';

function Workspace({ email, onSignOut }: { email: string; onSignOut: () => void }) {
  const { messages, busy, step, send, reset } = useChat();
  // Wide screens show planner and chat side by side; narrow screens show one at a time.
  const [planOpen, setPlanOpen] = useState(true);

  const plan = useCallback(
    (message: string) => {
      if (window.matchMedia(NARROW).matches) setPlanOpen(false);
      void send(message);
    },
    [send],
  );

  return (
    <div className="shell" data-plan={planOpen ? 'open' : 'closed'}>
      <aside className="plan" aria-label="Trip planner">
        <Planner busy={busy} onPlan={plan} onClose={() => setPlanOpen(false)} />
      </aside>
      <Chat
        messages={messages}
        busy={busy}
        step={step}
        email={email}
        planOpen={planOpen}
        onTogglePlan={() => setPlanOpen(open => !open)}
        onSend={text => void send(text)}
        onNewTrip={() => {
          reset();
          setPlanOpen(true);
        }}
        onSignOut={onSignOut}
      />
    </div>
  );
}

export default function App() {
  const [loggedIn, setLoggedIn] = useState(session.isLoggedIn);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    setSessionExpiredHandler(() => {
      setNotice('Your session expired. Sign in again to continue.');
      setLoggedIn(false);
    });
  }, []);

  if (!loggedIn) {
    return (
      <AuthScreen
        notice={notice}
        onSignedIn={() => {
          setNotice(null);
          setLoggedIn(true);
        }}
      />
    );
  }

  return (
    <Workspace
      email={session.email()}
      onSignOut={() => {
        signOut();
        setNotice(null);
        setLoggedIn(false);
      }}
    />
  );
}
