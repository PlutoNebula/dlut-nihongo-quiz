/**
 * importData 合并导入的集成测试（fake-indexeddb + 真实 Dexie）
 *
 * 回归覆盖：AUD-003 —— 合并导入的备份带有自增主键 id，
 * 同一份备份重复导入时不得触发主键冲突（ConstraintError）。
 */
import 'fake-indexeddb/auto'
import { describe, it, expect, beforeEach } from 'vitest'
import { db, exportData, importData } from './database'

const attempt = {
  id: 7,
  questionId: 'q-merge-1',
  sessionId: 'sess-x',
  selectedKey: 'A',
  correctKey: 'A',
  isCorrect: true,
  elapsedMs: 1234,
  mode: 'random',
  createdAt: '2026-01-01T00:00:00.000Z',
}

const session = {
  id: 3,
  mode: 'random',
  totalQuestions: 5,
  correctCount: 4,
  wrongCount: 1,
  startedAt: '2026-01-01T00:00:00.000Z',
}

async function resetDb() {
  await db.transaction('rw', db.tables, async () => {
    for (const t of db.tables) await t.clear()
  })
}

describe('importData merge（fake-indexeddb 集成）', () => {
  beforeEach(resetDb)

  it('首次合并导入成功，记录数正确', async () => {
    await importData(JSON.stringify({ version: 2, attempts: [attempt], sessions: [session] }), {
      merge: true,
    })
    expect(await db.attempts.count()).toBe(1)
    expect(await db.sessions.count()).toBe(1)
    const saved = await db.attempts.get(1)
    expect(saved?.questionId).toBe('q-merge-1')
  })

  it('同一份带主键的备份重复合并导入不报错，且记录追加而非冲突回滚', async () => {
    const backup = JSON.stringify({ version: 2, attempts: [attempt], sessions: [session] })
    await importData(backup, { merge: true })
    // 第二次导入与第一次完全相同 —— 修复前这里抛 ConstraintError 并整体回滚
    await expect(importData(backup, { merge: true })).resolves.toBeUndefined()
    expect(await db.attempts.count()).toBe(2)
    expect(await db.sessions.count()).toBe(2)
  })

  it('重复合并导入后导出的数据结构完整', async () => {
    const backup = JSON.stringify({ version: 2, attempts: [attempt] })
    await importData(backup, { merge: true })
    await importData(backup, { merge: true })
    const exported = JSON.parse(await exportData())
    expect(exported.attempts).toHaveLength(2)
    expect(exported.attempts.every((a: { id: number }) => typeof a.id === 'number')).toBe(true)
    expect(new Set(exported.attempts.map((a: { id: number }) => a.id)).size).toBe(2)
  })

  it('整体导入（非 merge）路径不受影响：clear + bulkPut 按 id 覆盖', async () => {
    const backup = JSON.stringify({ version: 2, attempts: [attempt] })
    await importData(backup) // 整体模式保留原主键
    await importData(backup)
    expect(await db.attempts.count()).toBe(1)
    expect((await db.attempts.get(7))?.questionId).toBe('q-merge-1')
  })
})
