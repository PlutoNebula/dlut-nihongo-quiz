<script setup lang="ts">
import { useRouter } from 'vue-router'

const router = useRouter()
</script>
<template>
  <div class="not-found">
    <p class="code">404</p>
    <p class="hint">页面不存在或已被移动</p>
    <button class="back-btn" @click="router.push('/')">返回首页</button>
  </div>
</template>
<style scoped>
.not-found {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  min-height: 50vh;
}
.code {
  font-size: 56px;
  font-weight: 700;
  color: var(--text-muted);
  letter-spacing: 2px;
  margin: 0;
}
.hint {
  color: var(--text-secondary);
  margin: 0;
}
.back-btn {
  margin-top: 8px;
  padding: 8px 20px;
  border: 1px solid var(--border);
  background: var(--bg-card);
  color: var(--text-primary);
  cursor: pointer;
  border-radius: 4px;
  font-family: inherit;
  font-size: 14px;
  transition:
    color 0.18s,
    border-color 0.18s;
}
.back-btn:hover {
  color: var(--accent);
  border-color: var(--accent);
}
</style>
