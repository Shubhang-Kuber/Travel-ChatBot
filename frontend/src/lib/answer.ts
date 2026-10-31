// The agent answers in plain text shaped like (agent/graph.py build_itinerary):
//
//   Itinerary for Lisbon
//
//   Day 1: Arrival
//   Summary sentence.
//   - Activity
//
//   Notes
//   - Something uncertain
//
// parseAnswer turns that into blocks the UI can style. Anything that doesn't match stays a paragraph,
// so free-form replies (follow-up questions, errors) render as they always did.

export type Block =
  | { kind: 'title'; text: string }
  | { kind: 'day'; number: string; title: string; summary: string[]; items: string[] }
  | { kind: 'notes'; items: string[] }
  | { kind: 'text'; text: string };

const TITLE = /^Itinerary(?: for (.+))?$/;
const DAY = /^Day (\d+)(?::\s*(.*))?$/;
const NOTES = /^Notes:?$/;
const BULLET = /^[-*•]\s+(.*)$/;

export function parseAnswer(raw: string): Block[] {
  const blocks: Block[] = [];
  let day: Extract<Block, { kind: 'day' }> | null = null;
  let notes: Extract<Block, { kind: 'notes' }> | null = null;
  let paragraph: string[] = [];

  const flushParagraph = () => {
    if (paragraph.length) blocks.push({ kind: 'text', text: paragraph.join('\n') });
    paragraph = [];
  };

  for (const line of raw.split('\n').map(l => l.trimEnd())) {
    const text = line.trim();
    const bullet = text.match(BULLET)?.[1];

    if (!text) {
      flushParagraph();
      continue;
    }
    const title = text.match(TITLE);
    const dayMatch = text.match(DAY);
    if (title && blocks.length === 0 && !paragraph.length) {
      blocks.push({ kind: 'title', text: title[1] ?? '' });
      continue;
    }
    if (dayMatch) {
      flushParagraph();
      notes = null;
      day = { kind: 'day', number: dayMatch[1], title: dayMatch[2] ?? '', summary: [], items: [] };
      blocks.push(day);
      continue;
    }
    if (NOTES.test(text)) {
      flushParagraph();
      day = null;
      notes = { kind: 'notes', items: [] };
      blocks.push(notes);
      continue;
    }
    if (bullet !== undefined && (day || notes)) {
      (notes ?? day)!.items.push(bullet);
      continue;
    }
    if (day && !notes) {
      day.summary.push(text);
      continue;
    }
    paragraph.push(text);
  }
  flushParagraph();
  return blocks;
}

export function isItinerary(blocks: Block[]): boolean {
  return blocks.some(b => b.kind === 'day');
}
