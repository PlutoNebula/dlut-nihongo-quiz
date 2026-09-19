import { test } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { COMPUTER_BANKS, validateSource } from './parse-computer-banks.mjs'

const readBank = (key) =>
  JSON.parse(
    fs.readFileSync(
      new URL(`../data/raw/computer-organization/${key}.json`, import.meta.url),
      'utf8',
    ),
  )

test('reviewed floating-point, MIPS and OCR corrections remain correct', () => {
  const questions = new Map(
    COMPUTER_BANKS.flatMap(({ key }) => readBank(key).questions.map((q) => [q.id, q])),
  )
  const float32 = (value) => {
    const buffer = Buffer.alloc(4)
    buffer.writeFloatBE(value)
    return buffer.readUInt32BE()
  }
  const cases = {
    'computer-2021-final-s06-q001': float32(3.5).toString(16).toUpperCase(),
    'computer-2021-final-s06-q002': float32(0.25).toString(16).toUpperCase(),
    'computer-2021-final-s06-q003': float32(-0.078125).toString(16).toUpperCase(),
    'computer-2021-final-s07-q002': ((2 << 26) | (0x0040001c >>> 2))
      .toString(16)
      .padStart(8, '0')
      .toUpperCase(),
    'computer-2021-final-s07-q003': (((35 << 26) | (16 << 21) | (8 << 16) | 8) >>> 0)
      .toString(16)
      .toUpperCase(),
    'computer-c-exam-s01-q002': String((800 + 4 * 4) >>> 2),
    'computer-c-exam-s06-q001': float32(-0.140625).toString(2).padStart(32, '0'),
    'computer-c-exam-s06-q002': float32(1.75).toString(2).padStart(32, '0'),
    'computer-midterms-s01-q026': (193).toString(2),
    'computer-midterms-s01-q031': (0b00111000).toString(16),
    'computer-midterms-s03-q031': (0b00111000).toString(16),
    'computer-2021-final-s04-q009': '桁落ち',
  }
  for (const [id, answer] of Object.entries(cases)) {
    assert.equal(questions.get(id)?.answerText, answer, id)
  }
})

const answeredBank = (key = 'computer-2021-final') => {
  const source = readBank(key)
  for (const q of source.questions) {
    if (q.questionType === 'written') q.questionType = 'fill'
    q.answerProvenance = 'generated'
    q.status = 'ready'
    q.explanation = '测试用解析'
    if (q.questionType === 'fill') {
      q.options = []
      q.answerKey = ''
      q.answerText = '测试用填空答案'
    } else {
      q.answerKey = q.options[0].key
      q.answerText = q.options[0].text
    }
  }
  return source
}

test('all 31 source pages and all questions remain traceable after splitting', () => {
  let pages = 0
  let questions = 0
  for (const { key, pages: count } of COMPUTER_BANKS) {
    const source = readBank(key)
    const result = validateSource(source, count)
    const generated = JSON.parse(
      fs.readFileSync(new URL(`../public/${key}-question-bank.json`, import.meta.url), 'utf8'),
    )
    assert.deepEqual(
      generated,
      source.questions,
      `${key}: generated data must match reviewed source`,
    )
    assert.ok(
      source.questions.every((q) => q.answerText.trim() && q.explanation?.trim()),
      `${key}: every question must have an answer and explanation`,
    )
    pages += result.pages
    questions += result.questions
  }
  assert.equal(pages, 31)
  assert.equal(questions, 199)
})

test('a handwritten selection cannot silently become a standard answer', () => {
  const source = answeredBank()
  source.questions[0].answerProvenance = 'handwritten'
  source.questions[0].answerKey = 'B'
  assert.throws(() => validateSource(source, 3), /unverified answer must stay empty/)
})

test('questions must belong to a registered sheet with a matching title', () => {
  for (const mutation of ['missing', 'unknown', 'non-string', 'wrong-title']) {
    const source = answeredBank()
    const q = source.questions[0]
    if (mutation === 'missing') {
      delete q.groupId
      delete q.groupTitle
    } else if (mutation === 'unknown') {
      q.groupId = 'unknown-sheet'
      delete q.groupTitle
    } else if (mutation === 'non-string') {
      source.groups.find((g) => g.id === q.groupId).id = 42
      q.groupId = 42
    } else {
      q.groupTitle = '错误的题单标题'
    }
    assert.throws(() => validateSource(source, 3), /unknown or mismatched sheet/, mutation)
  }
})

test('every answer provenance requires a string explanation', () => {
  for (const provenance of ['printed', 'none', 'handwritten', 'generated']) {
    for (const explanation of [undefined, null, 42, false, [], {}]) {
      const source = answeredBank()
      const q = source.questions[0]
      q.answerProvenance = provenance
      q.explanation = explanation
      if (provenance === 'none' || provenance === 'handwritten') {
        q.answerKey = ''
        q.answerText = ''
        q.status = 'needs_review'
      }
      assert.throws(
        () => validateSource(source, 3),
        /invalid explanation/,
        `${provenance}: ${JSON.stringify(explanation)}`,
      )
    }
  }
})

test('non-generated provenance may keep an empty explanation', () => {
  for (const provenance of ['printed', 'none', 'handwritten']) {
    const source = answeredBank()
    const q = source.questions[0]
    q.answerProvenance = provenance
    q.explanation = ''
    if (provenance === 'none' || provenance === 'handwritten') {
      q.answerKey = ''
      q.answerText = ''
      q.status = 'needs_review'
    }
    assert.doesNotThrow(() => validateSource(source, 3), provenance)
  }
})

test('missing source coverage is rejected even if question counts remain unchanged', () => {
  const source = answeredBank()
  source.coverage[0].questionIds.shift()
  assert.throws(() => validateSource(source, 3), /missing from coverage|not fully covered/)
})

test('the provider-blocked page cannot lose its review warning', () => {
  const source = answeredBank('computer-midterms')
  for (const q of source.questions.filter((q) => q.source.pages.includes(14))) q.reviewNotes = []
  assert.throws(() => validateSource(source, 19), /blocked OCR page needs an explicit review note/)
})

test('generated answers are accepted without changing their provenance', () => {
  for (const { key, pages } of COMPUTER_BANKS) {
    const source = answeredBank(key)
    assert.equal(validateSource(source, pages).needsReview, 0)
    assert.ok(source.questions.every((q) => q.answerProvenance === 'generated'))
  }
})

test('single and judgement answers must identify an existing option and match its text', () => {
  for (const type of ['single', 'judgement']) {
    for (const mutation of ['unknown-key', 'wrong-text']) {
      const source = answeredBank()
      const q = source.questions.find((q) => q.questionType === type)
      if (mutation === 'unknown-key') q.answerKey = 'Z'
      else q.answerText = '不是所选选项的文本'
      assert.throws(() => validateSource(source, 3), /answer option|answer text must match/)
    }
  }
})

test('generated answers and explanations cannot be blank', () => {
  for (const field of ['answerText', 'explanation']) {
    const source = answeredBank()
    source.questions[0][field] = '  '
    assert.throws(() => validateSource(source, 3), /empty answer text|empty explanation/)
  }
})

test('multi answers require unique valid keys and the multiAnswer flag', () => {
  const source = answeredBank()
  const q = source.questions[0]
  q.questionType = 'multi'
  q.multiAnswer = true
  q.answerKey = q.options
    .slice(0, 2)
    .map((o) => o.key)
    .join('')
  q.answerText = q.options
    .slice(0, 2)
    .map((o) => o.text)
    .join('；')
  assert.doesNotThrow(() => validateSource(source, 3))
  for (const keys of ['', 'AZ', 'AA']) {
    const invalid = structuredClone(source)
    invalid.questions[0].answerKey = keys
    assert.throws(() => validateSource(invalid, 3), /invalid multi answer keys/)
  }
  q.multiAnswer = false
  assert.throws(() => validateSource(source, 3), /multi answer flag/)
})

test('fill answers must contain text and have no options or answer key', () => {
  for (const field of ['answerText', 'answerKey', 'options']) {
    const source = answeredBank()
    const q = source.questions.find((q) => q.questionType === 'fill')
    if (field === 'answerText') q.answerText = ''
    if (field === 'answerKey') q.answerKey = 'A'
    if (field === 'options') q.options = [{ key: 'A', text: '多余选项' }]
    assert.throws(() => validateSource(source, 3), /empty answer text|invalid fill answer/)
  }
})

test('unverified provenance cannot carry an answer or be marked ready', () => {
  for (const provenance of ['none', 'handwritten']) {
    const source = answeredBank()
    const q = source.questions[0]
    q.answerProvenance = provenance
    assert.throws(() => validateSource(source, 3), /unverified answer must stay empty/)
    q.answerKey = ''
    q.answerText = ''
    assert.throws(() => validateSource(source, 3), /unverified question must need review/)
  }
})

test('written questions cannot introduce an unsupported question type', () => {
  const source = answeredBank()
  source.questions[0].questionType = 'written'
  assert.throws(() => validateSource(source, 3), /invalid type/)
})
