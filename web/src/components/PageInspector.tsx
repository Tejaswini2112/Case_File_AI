import { REFUSAL_THRESHOLD, type Hit } from '../api'

// Plain-language names for the internal doc_kind values. The API speaks in
// pipeline terms; a reader should not have to.
const KIND_LABEL: Record<string, string> = {
  'newspaper': 'Newspaper clipping',
  'court-opinion': 'Court opinion',
  'teletype': 'Teletype message',
  'form': 'FBI form',
  'deletion-sheet': 'Removal notice',
  'legal': 'Legal document',
  'cover': 'Cover sheet',
  'memo': 'Memorandum',
  'loose': 'Loose page',
}

function pageLabel(hit: Hit): string {
  if (!hit.page_nos.length) return 'page unknown'
  if (hit.page_nos.length === 1) return `page ${hit.page_nos[0]}`
  return `pages ${hit.page_nos[0]}–${hit.page_nos[hit.page_nos.length - 1]}`
}

/** The score with the refusal cut-off marked, so it reads as a position
 *  relative to a decision rather than as a bare number. */
function ScoreBar({ score }: { score: number }) {
  return (
    <div>
      <div className="flex justify-between items-baseline">
        <span className="text-sm">How closely it matches the question</span>
        <span className="font-mono text-sm">{score.toFixed(2)}</span>
      </div>
      <div className="relative h-[3px] bg-paper-2 mt-2.5">
        <div
          className="absolute inset-y-0 left-0 bg-ink"
          style={{ width: `${Math.min(score, 1) * 100}%` }}
        />
        <div
          className="absolute -top-1.5 -bottom-1.5 w-[2px] bg-mark"
          style={{ left: `${REFUSAL_THRESHOLD * 100}%` }}
          title={`Refusal cut-off (${REFUSAL_THRESHOLD})`}
        />
      </div>
      <p className="text-xs text-ink-faint mt-2 leading-relaxed">
        The red mark is the cut-off. Below it the system refuses to answer at all.
      </p>
    </div>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="text-sm text-ink-soft">{label}</dt>
      <dd className="text-sm text-right font-mono">{value}</dd>
    </>
  )
}

export default function PageInspector({ hit, onClose }: { hit: Hit; onClose: () => void }) {
  const kind = hit.doc_kind ?? 'unknown'

  return (
    <div className="h-full overflow-y-auto bg-paper-2 border-l border-rule px-9 py-10">
      <div className="flex items-start justify-between gap-4 mb-6 pb-4 border-b border-rule">
        <div>
          <div className="label-caps mb-1.5">Source</div>
          <h2 className="font-serif text-2xl leading-tight">
            {KIND_LABEL[kind] ?? kind}, {pageLabel(hit)}
          </h2>
        </div>
        <button
          onClick={onClose}
          className="text-xs text-ink-faint hover:text-mark shrink-0 mt-1"
        >
          Close
        </button>
      </div>

      {/* Deliberately empty. Rendering the scan needs page images from the
          source PDFs, which are not served yet; a labelled frame is honest,
          a stock image would not be. */}
      <div className="border border-rule bg-panel h-44 mb-7 flex items-end p-2">
        <span className="text-xs text-ink-faint">page image not yet available</span>
      </div>

      <div className="label-caps mb-2.5">What the page says</div>
      <pre className="bg-panel border border-rule p-4 text-xs font-mono
                      whitespace-pre-wrap leading-relaxed text-ink-soft">
        {hit.text}
      </pre>
      <p className="text-xs text-ink-faint mt-2 mb-8">
        This is the excerpt the model was given &mdash; not the whole page.
      </p>

      <div className="label-caps mb-3.5">How much to trust this</div>
      <ScoreBar score={hit.score} />

      <div className="label-caps mt-9 mb-3.5">Where this page came from</div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2.5">
        <Row label="Kind of document" value={KIND_LABEL[kind] ?? kind} />
        <Row label="Pages" value={hit.page_nos.join(', ') || '—'} />
        {hit.case_nums.length > 0 && (
          <Row label="Case number" value={hit.case_nums.join(', ')} />
        )}
        <Row label="Reference" value={hit.doc_id ?? '—'} />
      </dl>
    </div>
  )
}
