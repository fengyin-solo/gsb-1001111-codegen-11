<template>
  <section class="page" data-module="vehicle">
    <header class="page-head">
      <div>
        <h2>养护车辆管理</h2>
        <p class="page-desc">车辆按抢修包统一出车，汇总巡查路线、预计到达顺序和整包校验结论，避免重复派车。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记养护车辆</button>
        <button class="btn" type="button" @click="exportRows">导出养护车辆清单</button>
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

    <section class="task-panel">
      <h3>灭灯抢修车辆任务汇总</h3>
      <table class="data-table compact">
        <thead>
          <tr>
            <th>到达顺序</th>
            <th>车辆</th>
            <th>车牌 / 驾驶员</th>
            <th>抢修包</th>
            <th>阶段</th>
            <th>巡查路线</th>
            <th>预计到达</th>
            <th>校验结论</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="task in tasks" :key="String(task.id)">
            <td>第 {{ task.arrival_order }} 个</td>
            <td>{{ task.vehicle_no }}</td>
            <td>{{ task.plate_no }} / {{ task.driver }}</td>
            <td>{{ task.package_no }}</td>
            <td>{{ task.package_stage }}</td>
            <td>{{ task.route }}</td>
            <td>{{ task.estimated_arrival }}</td>
            <td>{{ task.validation_conclusion }}</td>
          </tr>
          <tr v-if="!tasks.length">
            <td colspan="8" class="empty-state">暂无灭灯抢修车辆任务</td>
          </tr>
        </tbody>
      </table>
    </section>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>当前抢修任务</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <strong>{{ row.当前抢修包 || '—' }}</strong>
            <p class="cell-note">{{ row.抢修任务汇总 || '暂无抢修包' }}</p>
          </td>
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
          <td :colspan="columns.length + 2" class="empty-state">暂无养护车辆数据，可先登记养护车辆</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条养护车辆记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/vehicle'
const columns = ["车辆编号", "车辆类型", "车牌号", "所属单位", "年检日期", "驾驶员", "当前里程", "车辆状态"]
const actions = ["派车出车", "收车归库", "送修车辆"]
const stats = ref([{"label": "在库车辆", "value": 0}, {"label": "出车车辆", "value": 0}, {"label": "抢修任务", "value": 0}])

const rows = ref<Row[]>([])
const tasks = ref<Row[]>([])
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
  errorMessage.value = '养护车辆登记入口尚未接入审批流'
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
      throw new Error(String(payload.message || '养护车辆动作未生效，请稍后重试'))
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '养护车辆操作失败'
  }
}

async function reloadTasks() {
  const response = await request(`${ENDPOINT}/lighting-tasks`)
  if (!response.ok) throw new Error('车辆任务汇总读取失败')
  const payload = await response.json() as Row
  tasks.value = (payload.items as Row[]) ?? []
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    await Promise.all([
      request(`${ENDPOINT}?${query}`).then(async (response) => {
        if (!response.ok) throw new Error('养护车辆列表读取失败')
        const payload = await response.json() as Row
        rows.value = (payload.items as Row[]) ?? []
        total.value = Number(payload.total ?? rows.value.length)
      }),
      reloadTasks(),
    ])
    stats.value = [
      { label: '在库车辆', value: rows.value.filter((row) => row.status === '在库').length },
      { label: '出车车辆', value: rows.value.filter((row) => row.status === '出车作业').length },
      { label: '抢修任务', value: tasks.value.length },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '养护车辆列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.task-panel {
  margin: 12px 0;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
}
.task-panel h3 { margin: 0 0 10px; }
.compact th,
.compact td { padding: 6px 8px; }
.cell-note { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
</style>
