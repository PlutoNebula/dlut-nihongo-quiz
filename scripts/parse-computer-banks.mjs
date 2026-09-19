/** Validate OCR question sources and answer provenance, then build the four banks. */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
export const COMPUTER_BANKS = [
  { key: 'computer-2021-final', pages: 3 },
  { key: 'computer-2024-final', pages: 4 },
  { key: 'computer-c-exam', pages: 5 },
  { key: 'computer-midterms', pages: 19 },
]

export function validateSource(bank, expectedPages) {
  const check = (ok, message) => {
    if (!ok) throw new Error(`${bank.category}: ${message}`)
  }
  check(Array.isArray(bank.questions) && bank.questions.length > 0, 'empty question bank')
  check(Array.isArray(bank.groups) && bank.groups.length > 0, 'missing question sheets')
  const groups = new Map(bank.groups.map((g) => [g.id, g.title]))
  check(groups.size === bank.groups.length, 'duplicate sheet IDs')
  const ids = new Set()
  for (const q of bank.questions) {
    check(typeof q.id === 'string' && q.id.startsWith(`${bank.category}-`), 'invalid question ID')
    check(!ids.has(q.id), `duplicate ID ${q.id}`)
    ids.add(q.id)
    check(q.category === bank.category, `${q.id}: wrong category`)
    check(groups.get(q.groupId) === q.groupTitle, `${q.id}: unknown or mismatched sheet`)
    check(typeof q.stem === 'string' && q.stem.trim(), `${q.id}: empty stem`)
    check(
      ['single', 'multi', 'judgement', 'fill'].includes(q.questionType),
      `${q.id}: invalid type`,
    )
    check(Array.isArray(q.options), `${q.id}: invalid options`)
    check(
      new Set(q.options.map((o) => o.key)).size === q.options.length,
      `${q.id}: duplicate option keys`,
    )
    check(
      q.options.every((o) => typeof o.key === 'string' && typeof o.text === 'string'),
      `${q.id}: invalid option`,
    )
    check(
      ['none', 'printed', 'handwritten', 'generated'].includes(q.answerProvenance),
      `${q.id}: missing answer provenance`,
    )
    check(
      typeof q.answerKey === 'string' && typeof q.answerText === 'string',
      `${q.id}: invalid answer`,
    )
    if (q.answerProvenance === 'none' || q.answerProvenance === 'handwritten') {
      check(q.answerKey === '' && q.answerText === '', `${q.id}: unverified answer must stay empty`)
      check(q.status === 'needs_review', `${q.id}: unverified question must need review`)
    } else {
      check(q.answerText.trim(), `${q.id}: empty answer text`)
      if (q.answerProvenance === 'generated') {
        check(
          typeof q.explanation === 'string' && q.explanation.trim(),
          `${q.id}: empty explanation`,
        )
      }
      if (q.questionType === 'fill') {
        check(q.answerKey === '' && q.options.length === 0, `${q.id}: invalid fill answer`)
      } else if (q.questionType === 'multi') {
        check(q.multiAnswer === true, `${q.id}: missing multi answer flag`)
        const keys = [...q.answerKey]
        check(
          keys.length > 0 &&
            new Set(keys).size === keys.length &&
            keys.every((key) => q.options.some((o) => o.key === key)),
          `${q.id}: invalid multi answer keys`,
        )
      } else {
        const option = q.options.find((o) => o.key === q.answerKey)
        check(option, `${q.id}: missing answer option`)
        check(q.answerText === option.text, `${q.id}: answer text must match selected option`)
      }
    }
    check(
      Array.isArray(q.source?.pages) && q.source.pages.length > 0,
      `${q.id}: missing source pages`,
    )
    check(
      q.source.pages.every((p) => Number.isInteger(p) && p >= 1 && p <= expectedPages),
      `${q.id}: source page out of range`,
    )
    if (bank.category === 'computer-midterms' && q.source.pages.includes(14)) {
      check(
        q.reviewNotes?.some((n) => /451|未.{0,5}核对|未复核/.test(n)),
        `${q.id}: blocked OCR page needs an explicit review note`,
      )
    }
  }
  check(
    Array.isArray(bank.coverage) && bank.coverage.length === expectedPages,
    'incomplete page coverage',
  )
  const coveredPages = new Set()
  const coveredQuestions = new Set()
  for (const row of bank.coverage) {
    check(
      Number.isInteger(row.page) &&
        row.page >= 1 &&
        row.page <= expectedPages &&
        !coveredPages.has(row.page),
      'duplicate or invalid coverage page',
    )
    coveredPages.add(row.page)
    check(Array.isArray(row.questionIds), `page ${row.page}: missing question mapping`)
    check(
      row.questionIds.length || row.nonQuestionContent?.trim(),
      `page ${row.page}: unexplained empty coverage`,
    )
    for (const id of row.questionIds) {
      check(ids.has(id), `page ${row.page}: unknown question ${id}`)
      check(
        bank.questions.find((q) => q.id === id).source.pages.includes(row.page),
        `${id}: coverage/source mismatch`,
      )
      coveredQuestions.add(id)
    }
  }
  check(coveredQuestions.size === ids.size, 'questions missing from coverage')
  for (const q of bank.questions) {
    check(
      q.source.pages.every((p) =>
        bank.coverage.find((row) => row.page === p).questionIds.includes(q.id),
      ),
      `${q.id}: source not fully covered`,
    )
  }
  return {
    category: bank.category,
    questions: ids.size,
    sheets: groups.size,
    pages: coveredPages.size,
    needsReview: bank.questions.filter((q) => q.status === 'needs_review').length,
  }
}

function main() {
  // Validate all inputs before writing any generated bank.
  const inputs = COMPUTER_BANKS.map(({ key, pages }) => {
    const source = JSON.parse(
      fs.readFileSync(path.join(root, 'data/raw/computer-organization', `${key}.json`), 'utf8'),
    )
    if (source.category !== key) throw new Error(`${key}: mismatched source`)
    const result = validateSource(source, pages)
    return { key, source, result }
  })
  for (const { key, source, result } of inputs) {
    fs.writeFileSync(
      path.join(root, 'public', `${key}-question-bank.json`),
      JSON.stringify(source.questions, null, 2) + '\n',
    )
    console.log(
      `${key}: ${result.questions} questions / ${result.sheets} sheets / ${result.pages} pages`,
    )
  }
  fs.mkdirSync(path.join(root, 'data/processed'), { recursive: true })
  fs.writeFileSync(
    path.join(root, 'data/processed/computer-validation-report.json'),
    JSON.stringify(
      inputs.map((i) => i.result),
      null,
      2,
    ) + '\n',
  )
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main()
