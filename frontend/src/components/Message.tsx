import { AnimatePresence, motion } from 'framer-motion';
import { Compass } from 'lucide-react';
import { isItinerary, parseAnswer, type Block } from '../lib/answer';
import { PROGRESS_STEPS, type ChatMessage } from '../hooks/useChat';
import { PlaneEast } from './Brand';

function Thinking({ step }: { step: number }) {
  return (
    <div className="thinking" role="status">
      <div className="track">
        <PlaneEast size={20} />
      </div>
      <AnimatePresence mode="wait">
        <motion.span
          key={step}
          className="thinking-label"
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -4 }}
          transition={{ duration: 0.2 }}
        >
          {PROGRESS_STEPS[step]}…
        </motion.span>
      </AnimatePresence>
    </div>
  );
}

function BlockView({ block }: { block: Block }) {
  switch (block.kind) {
    case 'title':
      return (
        <h2 className="it-title">
          <small className="mono">Itinerary</small>
          {block.text}
        </h2>
      );
    case 'day':
      return (
        <section className="day">
          <div className="day-stub">
            <span className="mono">Day</span>
            <b>{block.number}</b>
          </div>
          <div className="day-main">
            {block.title && <h3>{block.title}</h3>}
            {block.summary.map((line, i) => (
              <p key={i}>{line}</p>
            ))}
            {block.items.length > 0 && (
              <ul className="items">
                {block.items.map((item, i) => (
                  <li key={i}>{item}</li>
                ))}
              </ul>
            )}
          </div>
        </section>
      );
    case 'notes':
      return (
        <aside className="notes">
          <span className="mono">Notes</span>
          <ul className="items">
            {block.items.map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
        </aside>
      );
    case 'text':
      return <p className="ai-text">{block.text}</p>;
  }
}

export function Message({ message, step }: { message: ChatMessage; step: number }) {
  if (message.role === 'user') {
    return (
      <motion.div
        className="msg-user"
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.2 }}
      >
        {message.text}
      </motion.div>
    );
  }

  const blocks = message.text ? parseAnswer(message.text) : [];
  const typing = message.status === 'typing';

  return (
    <motion.div
      className="msg-ai"
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.25 }}
    >
      <span className="avatar" aria-hidden="true">
        <Compass size={18} />
      </span>
      <div className="ai-body">
        {message.status === 'waiting' && <Thinking step={step} />}
        {isItinerary(blocks)
          ? blocks.map((block, i) => <BlockView key={i} block={block} />)
          : message.text && (
              <p className="ai-text">
                {message.text}
                {typing && <span className="caret" />}
              </p>
            )}
        {message.error && (
          <p className="ai-error" role="alert">
            {message.error}
          </p>
        )}
      </div>
    </motion.div>
  );
}
