export interface Stop {
  id: number;
  destination: string;
  from: string; // yyyy-mm-dd
  to: string;
}

let nextId = 1;
export const newStop = (): Stop => ({ id: nextId++, destination: '', from: '', to: '' });

export const todayISO = () => new Date().toISOString().split('T')[0];

export function tripDays(from: string, to: string): number {
  return Math.round((new Date(to).getTime() - new Date(from).getTime()) / 86_400_000) + 1;
}

export function formatDate(iso: string): string {
  if (!iso) return '';
  return new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
}

/** Returns an error message for the first problem, or null when the stops are ready to send. */
export function validateStops(stops: Stop[]): string | null {
  const filled = stops.filter(s => s.destination.trim() && s.from && s.to);
  if (filled.length === 0) return 'Add at least one destination with dates.';
  const backwards = filled.find(s => s.to < s.from);
  if (backwards) return `The dates for ${backwards.destination.trim()} end before they start.`;
  return null;
}

export function itineraryMessage(stops: Stop[], notes: string): string {
  const lines = stops
    .filter(s => s.destination.trim() && s.from && s.to)
    .map((stop, i) => {
      const days = tripDays(stop.from, stop.to);
      return `${i + 1}. ${stop.destination.trim()}: ${stop.from} to ${stop.to} (${days} day${days === 1 ? '' : 's'})`;
    });
  let message = `Plan an itinerary for my trip:\n${lines.join('\n')}`;
  if (notes.trim()) message += `\nNotes: ${notes.trim()}`;
  return message;
}
