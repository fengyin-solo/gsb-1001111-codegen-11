<template>
  <section class="page" data-module="patrol">
    <header class="page-head">
      <div>
        <h2>日常巡查管理</h2>
        <p class="page-desc">灭灯抢修包会自动生成巡查清单，记录巡查路线、车辆到达顺序和整包校验结论。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记巡查记录</button>
        <button class="btn" type="button" @click="exportRows">导出日常巡查清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <section class="check-panel">
      <h3>灭灯抢修巡查清单</h3>
      <table class="data-table compact">
        <thead>
          <tr>
            <th>抢修包</th>
            <th>巡查路段</th>
            <th>巡查路线</th>
            <th>车辆到达顺序</th>
            <th>状态</th>
            <th>抢修校验结论</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in checklist" :key="String(item.id)">
            <td>{{ item.package_no }}</td>
            <td>{{ item.巡查路段 }}</td>
            <td>{{ item.巡查路线 }}</td>
            <td>{{ item.车辆到达顺序 }}</td>
            <td>{{ item.巡查状态 }}</td>
            <td>{{ item.抢修校验结论 }}</td>
          </tr>
          <tr v-if="!checklist.length">
            <td colspan="6" class="empty-state">暂无灭灯抢修巡查任务</td>
          </tr>
        </tbody>
      </table>
    </section>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无日常巡查数据，可先登记巡查记录</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条日常巡查记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/patrol'
const columns = ["巡查编号", "巡查路段", "巡查日期", "巡查人员", "巡查车辆", "发现问题", "处置措施", "巡查状态"]
const actions = ["开始巡查", "完成巡查", "复核确认"]
const stats = ref([{"label": "今日巡查", "value": 0}, {"label": "待巡查路段", "value": 0}, {"label": "灭灯巡查包", "value": 0}])

const rows = ref<Row[]>([])
const checklist = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '巡查记录登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json() as Row
    if (!response.ok || payload.ok === false) {
      throw new Error(String(payload.message || '日常巡查动作未生效，请稍后重试'))
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '日常巡查操作失败'
  }
}

async function reloadChecklist() {
  const response = await request(`${ENDPOINT}/lighting-checklist`)
  if (!response.ok) throw new Error('灭灯巡查清单读取失败')
  const payload = await response.json() as Row
  checklist.value = (payload.items as Row[]) ?? []
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const listResponse = await request(`${ENDPOINT}?${query}`)
    await reloadChecklist()
    if (!listResponse.ok) {
      throw new Error('巡查记录列表读取失败')
    }
    const payload = await listResponse.json() as Row
    rows.value = (payload.items as Row[]) ?? []
    total.value = Number(payload.total ?? rows.value.length)
    stats.value = [
      { label: '今日巡查', value: total.value },
      { label: '待巡查路段', value: rows.value.filter((row) => row.status === '待巡查').length },
      { label: '灭灯巡查包', value: checklist.value.length },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '日常巡查列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.check-panel {
  margin: 12px 0;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
}
.check-panel h3 { margin: 0 0 10px; }
.compact th,
.compact td { padding: 6px 8px; }
</style>
