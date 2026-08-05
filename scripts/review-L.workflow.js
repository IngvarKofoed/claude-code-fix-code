export const meta = {
  name: 'fix-code-L',
  description: 'Review a large working diff across two lenses, verify each finding, sweep for gaps',
  phases: [
    { title: 'Find', detail: 'one worker per lens-slice' },
    { title: 'Verify', detail: 'refute each finding independently' },
    { title: 'Gap sweep', detail: 'what did no slice look at' },
  ],
}

// Schemas live here rather than being invented per run. An invented schema the
// workers can't satisfy fails validation, retries, and burns the whole budget
// before producing anything.
const FINDINGS = {
  type: 'object',
  required: ['findings'],
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['title', 'description', 'locations', 'severity', 'trace'],
        properties: {
          title: { type: 'string', maxLength: 60 },
          description: { type: 'string', maxLength: 320 },
          locations: {
            type: 'array', minItems: 1,
            items: {
              type: 'object',
              required: ['file', 'line'],
              properties: { file: { type: 'string' }, line: { type: 'integer' } },
            },
          },
          severity: { type: 'integer', minimum: 1, maximum: 5 },
          trace: { type: 'string' },
          fix: { type: 'string' },
          side_effects: { type: 'string' },
          unambiguous: { type: 'boolean' },
          // Required in practice whenever unambiguous is false: a finding left
          // for the author has to arrive with its options named, or the report
          // costs them a round trip to ask why it wasn't fixed.
          alternatives: { type: 'array', items: { type: 'string' } },
          recommended: { type: 'string' },
          // How much work is left once the author picks: true means one question
          // unblocks a small edit (the session asks), false means it's new design
          // either way (leave it alone). Without this the session can't tell the
          // two apart and defaults to leaving both.
          small_edit_once_decided: { type: 'boolean' },
        },
      },
    },
  },
}

const VERDICT = {
  type: 'object',
  required: ['real', 'reason'],
  properties: {
    real: { type: 'boolean' },
    reason: { type: 'string' },
    mitigation_read: { type: 'string' },
    severity: { type: 'integer', minimum: 1, maximum: 5 },
    unambiguous: { type: 'boolean' },
  },
}

const DISCIPLINES = `
Read this way, not by default code-review instinct:
1. Intent first, from a source that is NOT the diff. ${''}
2. Read the post-image whole, never the hunk alone.
3. Hunt absence: for every construct added, what do its peers do that it doesn't?
   Check one level out before flagging — the guard may exist off-hunk.
4. Read deletions hardest. Every removed guard is a candidate until proven safe.
5. Trace concrete values to an observable wrong result. Cross file boundaries.
   Never terminate a trace at a claim ("validated upstream").
6. Spend attention unevenly: boundaries, changed/unchanged seams, error paths,
   anything touching persisted state.
7. This is an UNCOMMITTED tree. Half-built is allowed and is not a defect. An
   absence is a finding only when an already-wired path misbehaves because of it.
8. Ask what this language/framework makes easy to get wrong, and bring those.

A finding needs a traced failure scenario or it does not exist. Drop anything a
linter would catch, anything on untouched lines, anything you can only call a
discomfort.`

const CAP = 6                     // hard ceiling on find-workers per run

// Compared against `budget.remaining()`, which counts OUTPUT tokens only. An
// estimate, and inert unless a per-turn budget is set. There is deliberately no
// total-consumption figure: it would be guesswork, and summing it across a
// mixed Opus/Sonnet fan-out gives a number matching no real cost.
const PER_WORKER_OUTPUT = 12_000

function plan(slices) {
  if (!budget.total) return { run: slices.slice(0, CAP), dropped: slices.slice(CAP) }
  const affordable = Math.max(1, Math.floor((budget.remaining() * 0.7) / PER_WORKER_OUTPUT))
  const n = Math.min(CAP, affordable, slices.length)
  return { run: slices.slice(0, n), dropped: slices.slice(n) }
}

// `args` reaches a workflow verbatim, and some hosts deliver it JSON-ENCODED rather than
// as a value — in which case `args.slices` is undefined and the run ends with zero agents
// and an "empty review" that reads as "nothing found". Accept either form.
const _args = typeof args === 'string'
  ? (() => { try { return JSON.parse(args) } catch (_) { return {} } })()
  : (args ?? {})
const slices = _args?.slices ?? []
if (!slices.length) return { error: 'no slices supplied', findings: [] }

const { run, dropped } = plan(slices)
if (dropped.length) {
  log(`Budget covers ${run.length} of ${slices.length} slices — NOT reviewing: ` +
      dropped.map(s => `${s.lens}/${s.label}`).join(', '))
}

phase('Find')
const perSlice = await pipeline(
  run,
  (s) => agent(
    `${DISCIPLINES}\n\nLens: ${s.lens}\nSlice: ${s.label}\n` +
    `Files:\n${(s.files ?? []).join('\n')}\n\n` +
    `Review ONLY the changed lines in these files (plus callers the change broke). ` +
    `Return findings, each with a traced failure scenario, 1-5 severity by ` +
    `consequence, every location the defect appears, and the fix.\n\n` +
    `On "unambiguous": the question is the smallest edit that stops the traced ` +
    `failure, NOT the best design for this code. Candidates that behave ` +
    `identically on every input and differ only in HOW THEY ARE WRITTEN (where ` +
    `the guard sits, early return vs. wrapped branch, which helper) are ONE fix ` +
    `with several drafts — set it true and pick the draft matching the ` +
    `surrounding code. Several files for one decision is still true. Set it false ` +
    `only when the candidates BEHAVE differently and which behavior is right is ` +
    `the author's call; then name them in "alternatives" and your pick in ` +
    `"recommended".\n\n` +
    `When unambiguous is false, also set "small_edit_once_decided": true if only ` +
    `a small edit is left once the author picks a behavior (a different ` +
    `comparison, one more branch, a changed default) — those get ONE question and ` +
    `then get fixed. False if new design is left either way (a new abstraction, a ` +
    `signature change, edits across subsystems) — getting an answer does not make ` +
    `a big edit small, so those are left alone.\n\n` +
    `Never lower severity because a finding has several candidate fixes. ` +
    `Severity is consequence; alternatives are a property of the repair.`,
    { label: `find:${s.lens}/${s.label}`, phase: 'Find',
      // plan.py resolved this; the fallback keeps the same rule so the
      // pre-flight table can never disagree with what actually runs.
      model: s.model ?? (s.risk ? 'opus' : 'sonnet'), schema: FINDINGS }
  ),
  // Verify as soon as a slice lands — no barrier, so a later slice dying does
  // not discard an earlier slice's verified findings.
  (res, s) => parallel((res?.findings ?? []).map((f) => () =>
    agent(
      `Try to REFUTE this finding. Default to "not real".\n\n` +
      `${f.title}\n${f.description}\nAt: ${f.locations.map(l => `${l.file}:${l.line}`).join(', ')}\n` +
      `Claimed trace: ${f.trace}\n\n` +
      `You may only refute with a mitigation you LOCATED AND READ. A comment ` +
      `asserting safety is not a mitigation; "the framework probably handles ` +
      `this" is not a mitigation. Killing a real finding with an imagined ` +
      `defense is as bad as inventing one. Judge the finding as written — a ` +
      `different bug nearby does not make this one true.\n\n` +
      `If you set "unambiguous", apply the same behavioral test: false only when ` +
      `the candidate fixes BEHAVE differently and the choice is the author's. ` +
      `Fixes differing only in how they are written, or spanning several files ` +
      `for one decision, are unambiguous. Do not fail a real fix on design taste.`,
      { label: `verify:${f.locations[0]?.file ?? s.label}`, phase: 'Verify',
        model: 'sonnet', schema: VERDICT }
    ).then((v) => ({ ...f, verdict: v }))
  ))
)

const survived = perSlice.flat().filter(Boolean).filter((f) => f.verdict?.real)
const lost = run.filter((_, i) => perSlice[i] === null)
if (lost.length) {
  log(`${lost.length} slice(s) produced nothing (died or errored): ` +
      lost.map(s => `${s.lens}/${s.label}`).join(', '))
}

// Gap sweep only if there's budget left for it — it's the cheapest way to learn
// what the slices missed, but it's worthless if it dies halfway.
let gaps = null
if (!budget.total || budget.remaining() > PER_WORKER_OUTPUT) {
  phase('Gap sweep')
  gaps = await agent(
    `These slices were reviewed: ${run.map(s => `${s.lens}/${s.label}`).join(', ')}.\n` +
    `${dropped.length ? `NOT reviewed: ${dropped.map(s => `${s.lens}/${s.label}`).join(', ')}.\n` : ''}` +
    `Confirmed findings:\n${survived.map(f => `- [${f.severity}] ${f.title}`).join('\n') || '(none)'}\n\n` +
    `What did no slice actually look at? Name changed files or behaviors with no ` +
    `finding and no evidence anyone read them. Do not restate findings.`,
    { label: 'gap-sweep', phase: 'Gap sweep', model: 'opus', schema: FINDINGS }
  )
} else {
  log('Skipping gap sweep — insufficient budget remaining.')
}

// Coverage is reported, never implied. A truncated run that looks complete is
// worse than one that says what it missed.
return {
  findings: survived,
  gap_candidates: gaps?.findings ?? [],
  coverage: {
    slices_total: slices.length,
    slices_reviewed: run.length - lost.length,
    slices_dropped_for_budget: dropped.map(s => `${s.lens}/${s.label}`),
    slices_lost_to_errors: lost.map(s => `${s.lens}/${s.label}`),
    gap_sweep_ran: gaps !== null,
    partial: dropped.length > 0 || lost.length > 0 || gaps === null,
  },
}
