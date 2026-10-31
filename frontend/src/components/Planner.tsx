import { useState, type FormEvent } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { Plus, Sparkles, X } from 'lucide-react';
import { itineraryMessage, newStop, todayISO, tripDays, validateStops, type Stop } from '../lib/itinerary';

interface Props {
  busy: boolean;
  onPlan: (message: string) => void;
  onClose: () => void;
}

export function Planner({ busy, onPlan, onClose }: Props) {
  const [stops, setStops] = useState<Stop[]>(() => [newStop()]);
  const [notes, setNotes] = useState('');
  const [error, setError] = useState<string | null>(null);

  const update = (id: number, change: Partial<Stop>) => {
    setError(null);
    setStops(list => list.map(s => (s.id === id ? { ...s, ...change } : s)));
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    const problem = validateStops(stops);
    if (problem) {
      setError(problem);
      return;
    }
    setError(null);
    onPlan(itineraryMessage(stops, notes));
  };

  const today = todayISO();

  return (
    <div className="plan-scroll">
      <div className="plan-head">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'start' }}>
          <h2>Plan your journey</h2>
          <button type="button" className="icon-btn plan-close" onClick={onClose} aria-label="Hide trip planner">
            <X size={20} />
          </button>
        </div>
        <p>Add your stops in order. TravelBot builds the itinerary from them.</p>
      </div>

      <form className="plan-form" onSubmit={submit}>
        <ol className="route">
          <AnimatePresence initial={false}>
            {stops.map((stop, index) => {
              const days = stop.from && stop.to && stop.to >= stop.from ? tripDays(stop.from, stop.to) : 0;
              return (
                <motion.li
                  key={stop.id}
                  className="stop"
                  layout
                  initial={{ opacity: 0, y: 14 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, x: -20 }}
                  transition={{ duration: 0.25 }}
                >
                  <span className="waypoint" aria-hidden="true">
                    {index + 1}
                  </span>
                  <div className="stop-card">
                    <label className="field">
                      <span>Stop {index + 1} destination</span>
                      <input
                        className="input"
                        value={stop.destination}
                        onChange={e => update(stop.id, { destination: e.target.value })}
                        placeholder="e.g. Paris, France"
                        required
                      />
                    </label>
                    <div className="stop-dates">
                      <label className="field">
                        <span>Arrive</span>
                        <input
                          className="input"
                          type="date"
                          min={today}
                          value={stop.from}
                          onChange={e => update(stop.id, { from: e.target.value })}
                          required
                        />
                      </label>
                      <label className="field">
                        <span>Leave</span>
                        <input
                          className="input"
                          type="date"
                          min={stop.from || today}
                          value={stop.to}
                          onChange={e => update(stop.id, { to: e.target.value })}
                          required
                        />
                      </label>
                    </div>
                    {(days > 0 || stops.length > 1) && (
                      <div className="stop-foot">
                        {days > 0 ? <span className="chip">{days} {days === 1 ? 'day' : 'days'}</span> : <span />}
                        {stops.length > 1 && (
                          <button
                            type="button"
                            className="link-btn"
                            onClick={() => setStops(list => list.filter(s => s.id !== stop.id))}
                          >
                            Remove stop {index + 1}
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                </motion.li>
              );
            })}
          </AnimatePresence>
        </ol>

        <button
          type="button"
          className="btn btn-ghost"
          onClick={() => setStops(list => [...list, newStop()])}
        >
          <Plus size={18} /> Add another stop
        </button>

        <label className="field">
          <span>Interests or questions (optional)</span>
          <textarea
            className="input"
            rows={3}
            maxLength={1000}
            value={notes}
            onChange={e => setNotes(e.target.value)}
            placeholder="e.g. budget trip, love street food, getting around by train"
          />
        </label>

        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}

        <button type="submit" className="btn btn-primary btn-block" disabled={busy}>
          <Sparkles size={18} /> Build my itinerary
        </button>
      </form>
    </div>
  );
}
