import { useCallback, useEffect, useRef, useState } from 'react';
import { describeError, session, streamChat } from '../api/client';

// Shown in turn while the agent works, so the wait reads as progress.
export const PROGRESS_STEPS = [
  'Understanding your request',
  'Searching travel tips',
  'Checking the details',
  'Writing your answer',
];
const PROGRESS_STEP_MS = 3500;

export interface ChatMessage {
  id: number;
  role: 'user' | 'assistant';
  text: string;
  /** waiting: no text yet. typing: text is arriving. done / error: finished. */
  status: 'waiting' | 'typing' | 'done' | 'error';
  error?: string;
}

let nextId = 1;

export function useChat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const [step, setStep] = useState(0);

  const busyRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);

  // The answer can arrive in one chunk, so text is revealed a few characters per frame instead of all at once.
  const queueRef = useRef('');
  const frameRef = useRef<number | null>(null);
  const targetRef = useRef<number | null>(null);
  const drainedRef = useRef<Array<() => void>>([]);

  const patch = useCallback((id: number, change: (m: ChatMessage) => Partial<ChatMessage>) => {
    setMessages(list => list.map(m => (m.id === id ? { ...m, ...change(m) } : m)));
  }, []);

  const typeFrame = useCallback(() => {
    const id = targetRef.current;
    const queue = queueRef.current;
    if (id == null || !queue) {
      frameRef.current = null;
      drainedRef.current.forEach(resolve => resolve());
      drainedRef.current = [];
      return;
    }
    const n = Math.max(3, Math.ceil(queue.length / 6));
    const chunk = queue.slice(0, n);
    queueRef.current = queue.slice(n);
    patch(id, m => ({ text: m.text + chunk }));
    frameRef.current = requestAnimationFrame(typeFrame);
  }, [patch]);

  const enqueue = useCallback(
    (id: number, text: string) => {
      targetRef.current = id;
      queueRef.current += text;
      patch(id, () => ({ status: 'typing' }));
      if (frameRef.current == null) frameRef.current = requestAnimationFrame(typeFrame);
    },
    [patch, typeFrame],
  );

  const drained = () =>
    new Promise<void>(resolve => {
      if (frameRef.current == null && !queueRef.current) resolve();
      else drainedRef.current.push(resolve);
    });

  // Advance the "what is the agent doing" label while waiting for the first token.
  const waiting = busy && messages.at(-1)?.status === 'waiting';
  useEffect(() => {
    if (!waiting) return;
    setStep(0);
    const timer = setInterval(() => setStep(s => Math.min(s + 1, PROGRESS_STEPS.length - 1)), PROGRESS_STEP_MS);
    return () => clearInterval(timer);
  }, [waiting]);

  const send = useCallback(
    async (text: string, displayText = text) => {
      if (busyRef.current || !text.trim()) return;
      busyRef.current = true;
      setBusy(true);

      const replyId = nextId++;
      setMessages(list => [
        ...list,
        { id: nextId++, role: 'user', text: displayText, status: 'done' },
        { id: replyId, role: 'assistant', text: '', status: 'waiting' },
      ]);

      const abort = new AbortController();
      abortRef.current = abort;
      let received = false;
      let failure: string | undefined;
      try {
        await streamChat(
          text,
          token => {
            received = true;
            enqueue(replyId, token);
          },
          abort.signal,
        );
        if (!received) enqueue(replyId, '(no answer was returned)');
      } catch (err) {
        if (abort.signal.aborted) return;
        failure = describeError(err);
      } finally {
        if (!abort.signal.aborted) {
          await drained();
          patch(replyId, () => (failure ? { status: 'error', error: failure } : { status: 'done' }));
          busyRef.current = false;
          setBusy(false);
        }
      }
    },
    [enqueue, patch],
  );

  const reset = useCallback(() => {
    if (busyRef.current) return;
    session.setThreadId(null);
    setMessages([]);
  }, []);

  useEffect(() => () => {
    abortRef.current?.abort();
    if (frameRef.current != null) cancelAnimationFrame(frameRef.current);
  }, []);

  return { messages, busy, step, send, reset };
}
