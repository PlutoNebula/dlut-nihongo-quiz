/**
 * Red Team 对抗测试：importData 对恶意/损坏备份的防御
 *
 * 背景（red team 发现）：merge 导入对数组元素不做逐条形状校验——
 *   attempts: [5] 静默入库一条空记录（数据污染）；
 *   attemptCount: "5" 与现有数值合并变字符串拼接（10 + "5" = "105"，统计永久损坏）；
 *   settings 缺 key 泄漏 Dexie 内部错误。
 * 修复：importData 入口逐条校验关键字段类型，fail-closed。
 *
 * 运行: npx vitest run src/db/redteam.import.test.ts
 */
import 'fake-indexeddb/auto'
import { describe, it, expect, beforeEach } from 'vitest'
import { db, exportData, importData } from './database'

async function resetDb() {
  await db.transaction('rw', db.tables, async () => {
    for (const t of db.tables) await t.clear()
  })
}

const stat = {
  questionId: 'q-1',
  attemptCount: 10,
  correctCount: 8,
  wrongCount: 2,
  masteryLevel: 3,
  lastSelectedKey: 'A',
  lastCorrect: true,
  lastAttemptAt: '2026-01-01T00:00:00.000Z',
  reviewDueAt: '2026-01-04T00:00:00.000Z',
  isBookmarked: false,
}

describe('redteam: 恶意备份导入必须 fail-closed', () => {
  beforeEach(resetDb)

  it('attempts 含数字元素：拒绝且不落空记录', async () => {
    await expect(
      importData(JSON.stringify({ version: 2, attempts: [5] }), { merge: true }),
    ).rejects.toThrow('备份格式错误')
    expect(await db.attempts.count()).toBe(0)
  })

  it('attempts 含 null 元素：拒绝且不泄漏内部解构错误', async () => {
    await expect(
      importData(JSON.stringify({ version: 2, attempts: [null] }), { merge: true }),
    ).rejects.toThrow('备份格式错误')
    expect(await db.attempts.count()).toBe(0)
  })

  it('questionStats 数值字段为字符串：拒绝（防拼接污染 10 + "5" = "105"）', async () => {
    await db.questionStats.put(stat)
    const evil = { ...stat, attemptCount: '5', correctCount: '3', wrongCount: '1' }
    await expect(
      importData(JSON.stringify({ version: 2, questionStats: [evil] }), { merge: true }),
    ).rejects.toThrow('备份格式错误')
    // 现有统计必须原样保留
    const kept = await db.questionStats.get('q-1')
    expect(kept?.attemptCount).toBe(10)
    expect(typeof kept?.attemptCount).toBe('number')
  })

  it('settings 缺 key 或 value 非字符串：拒绝且报错可读', async () => {
    await expect(
      importData(JSON.stringify({ version: 2, settings: [{ value: 'v' }] }), { merge: true }),
    ).rejects.toThrow('备份格式错误')
    await expect(
      importData(JSON.stringify({ version: 2, settings: [{ key: 'k', value: 1 }] }), {
        merge: true,
      }),
    ).rejects.toThrow('备份格式错误')
  })

  it('tagStats 缺数值字段：拒绝', async () => {
    await expect(
      importData(JSON.stringify({ version: 2, tagStats: [{ tag: '语法' }] }), { merge: true }),
    ).rejects.toThrow('备份格式错误')
  })

  it('损坏 JSON：拒绝且现有数据不变', async () => {
    await db.questionStats.put(stat)
    await expect(importData('{broken', { merge: true })).rejects.toThrow(
      '备份文件不是有效的 JSON',
    )
    expect((await db.questionStats.get('q-1'))?.attemptCount).toBe(10)
  })

  it('合法备份（exportData 输出形状）不受校验影响', async () => {
    await db.attempts.add({
      questionId: 'q-1',
      sessionId: 's-1',
      selectedKey: 'A',
      correctKey: 'A',
      isCorrect: true,
      elapsedMs: 100,
      mode: 'random',
      createdAt: '2026-01-01T00:00:00.000Z',
    })
    await db.questionStats.put(stat)
    const backup = await exportData()
    await resetDb()
    await expect(importData(backup, { merge: true })).resolves.toBeUndefined()
    expect(await db.attempts.count()).toBe(1)
    expect((await db.questionStats.get('q-1'))?.attemptCount).toBe(10)
  })
})

describe('redteam: merge 混合集（部分重复部分新增）', () => {
  beforeEach(resetDb)

  it('重复项数值累加、新增项原样插入，且不污染原有记录', async () => {
    await db.questionStats.put(stat)
    const imported = [
      { ...stat }, // 重复：应与现有累加
      { ...stat, questionId: 'q-new', attemptCount: 1, correctCount: 0, wrongCount: 1, masteryLevel: 1 }, // 新增
    ]
    await importData(JSON.stringify({ version: 2, questionStats: imported }), { merge: true })
    const merged = await db.questionStats.get('q-1')
    expect(merged?.attemptCount).toBe(20) // 10 + 10
    expect(merged?.correctCount).toBe(16)
    const added = await db.questionStats.get('q-new')
    expect(added?.attemptCount).toBe(1)
    expect((await db.questionStats.toArray()).length).toBe(2)
  })

  it('attempts 追加去主键重分配，原记录不受导入污染', async () => {
    await db.attempts.add({
      questionId: 'q-local',
      sessionId: 's-local',
      selectedKey: 'B',
      correctKey: 'A',
      isCorrect: false,
      elapsedMs: 50,
      mode: 'random',
      createdAt: '2026-01-02T00:00:00.000Z',
    })
    await importData(
      JSON.stringify({
        version: 2,
        attempts: [
          {
            questionId: 'q-remote',
            sessionId: 's-remote',
            selectedKey: 'A',
            correctKey: 'A',
            isCorrect: true,
            elapsedMs: 80,
            mode: 'random',
            createdAt: '2026-01-01T00:00:00.000Z',
          },
        ],
      }),
      { merge: true },
    )
    expect(await db.attempts.count()).toBe(2)
    const local = (await db.attempts.toArray()).find((a) => a.questionId === 'q-local')
    expect(local?.selectedKey).toBe('B')
    const importedRow = (await db.attempts.toArray()).find((a) => a.questionId === 'q-remote')
    expect(importedRow?.id).toBeGreaterThan(0) // 主键由 Dexie 重新分配
  })
})
