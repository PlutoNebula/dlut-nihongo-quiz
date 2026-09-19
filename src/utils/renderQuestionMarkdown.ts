import { Marked } from 'marked'
import markedKatex from 'marked-katex-extension'
import { sanitizeHtml } from './renderMarkdown'
import 'katex/dist/katex.min.css'

const renderer = new Marked({ gfm: true, breaks: false })
renderer.use(markedKatex({ throwOnError: false, nonStandard: true }))
// MIPS register names use a literal dollar sign; two registers are not a math span.
renderer.use({
  extensions: [
    {
      name: 'mipsRegister',
      level: 'inline',
      start: (source) => source.indexOf('$'),
      tokenizer(source) {
        const match = /^\$(?:zero|at|v[01]|a[0-3]|t[0-9]|s[0-8]|k[01]|gp|sp|fp|ra)\b(?!\$)/.exec(
          source,
        )
        if (match) return { type: 'mipsRegister', raw: match[0] }
      },
      renderer: (token) => token.raw,
    },
  ],
})

/** Preserve code blocks/tables and render OCR formulas without trusting source HTML. */
export function renderQuestionMarkdown(markdown: string): string {
  return sanitizeHtml(renderer.parse(markdown, { async: false }) as string)
}

/** Render answer text inside an inline result without adding paragraph wrappers. */
export function renderQuestionMarkdownInline(markdown: string): string {
  return sanitizeHtml(renderer.parseInline(markdown, { async: false }) as string)
}
