// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { renderQuestionMarkdown } from './renderQuestionMarkdown'

describe('computer question rendering', () => {
  it('preserves assembly formatting, tables, and mathematical notation', () => {
    const html = renderQuestionMarkdown(
      '```asm\nMOV AX, BX\nADD AX, 1\n```\n\n| 位 | 值 |\n|---|---|\n| 0 | 1 |\n\n$2^{16}$',
    )
    expect(html).toContain('MOV AX, BX\nADD AX, 1')
    expect(html).toContain('<table>')
    expect(html).toContain('class="katex"')
  })
  it('does not interpret MIPS register names as paired dollar math', () => {
    const html = renderQuestionMarkdown(
      'レジスタ$s0, $s1, $s2, $s3を使用。容量は$10^6$バイト。\n\n```asm\nadd $s0, $s1, $s2\n```',
    )
    expect(html).toContain('レジスタ$s0, $s1, $s2, $s3を使用。')
    expect(html).toContain('add $s0, $s1, $s2')
    expect(html.match(/class="katex"/g)).toHaveLength(1)
  })
  it('sanitizes raw OCR HTML while retaining content', () => {
    const html = renderQuestionMarkdown(
      '<img src="x" onerror="alert(1)"><script>alert(1)</script>题目',
    )
    expect(html).not.toContain('onerror')
    expect(html).not.toContain('<script')
    expect(html).toContain('题目')
  })
})
