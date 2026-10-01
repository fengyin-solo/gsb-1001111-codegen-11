<template>
  <section class="page" data-module="lighting">
    <header class="page-head">
      <div>
        <h2>路灯照明管理</h2>
        <p class="page-desc">灭灯故障按相邻杆号、回路和供电区聚合为抢修包；整包校验结论同步台账、巡查清单和车辆任务。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="recomputePackages">增量重算抢修包</button>
        <button class="btn" type="button" @click="exportRows">导出路灯照明清单</button>
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

    <section class="repair-panel">
      <div class="panel-head">
        <div>
          <h3>灭灯区域聚合与抢修编排</h3>
          <p>同一写法覆盖供电区、回路、道路、杆号范围、灯具数量和通行安全级别。</p>
        </div>
        <button class="btn ghost" type="button" @click="reloadAll">刷新聚合结果</button>
      </div>
      <div v-if="!packages.length" class="empty-state repair-empty">当前没有活动灭灯抢修包</div>
      <article v-for="pkg in packages" :key="String(pkg.id)" class="package-card">
        <header class="package-head">
          <div>
            <strong>{{ pkg.package_no }}</strong>
            <span class="stage-tag">{{ pkg.stage }}</span>
            <span class="stage-tag safety">{{ pkg.safety_level }}</span>
          </div>
          <div class="package-actions">
            <button class="link" type="button" @click="dispatch(pkg, [1, 2], true)">整包派单（VEHI-0001/0002）</button>
            <button class="link" type="button" @click="dispatch(pkg, [1, 2], false)">模拟线路图失败</button>
            <button class="link" type="button" @click="restore(pkg, true, [])">登记整包复电</button>
            <button class="link" type="button" @click="restore(pkg, true, firstTwoLampIds(pkg))">登记部分复电</button>
            <button class="link" type="button" @click="restore(pkg, false, [])">模拟复电失败</button>
          </div>
        </header>
        <p class="unified-fault">{{ pkg.unified_fault }}</p>
        <div class="route-grid">
          <div>
            <span>巡查路线</span>
            <strong>{{ formatList(pkg.patrol_route, ' → ') }}</strong>
          </div>
          <div>
            <span>车辆到达顺序</span>
            <strong>{{ formatArrival(pkg.arrival_order) }}</strong>
          </div>
          <div>
            <span>合并灯具</span>
            <strong>{{ formatLamps(pkg.lamp_ids) }}</strong>
          </div>
        </div>
        <p class="conclusion">抢修校验结论：{{ pkg.validation_conclusion }}</p>
      </article>
    </section>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>抢修包 / 校验结论</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <strong>{{ row['抢修包编号'] || '—' }}</strong>
            <p class="cell-note">{{ row['抢修校验结论'] || '未进入抢修包' }}</p>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="runAction('登记故障', row)">登记故障并聚合</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无路灯照明数据</td>
        </tr>
      </tbody>
    </table>

    <section class="history-panel">
      <h3>历史不亮记录（按每次复电结果保留）</h3>
      <table class="data-table compact">
        <thead>
          <tr>
            <th>灯具</th>
            <th>抢修包</th>
            <th>上报时间</th>
            <th>复电时间</th>
            <th>本次结果</th>
            <th>备注</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in history" :key="String(item.id)">
            <td>{{ item.lamp_no }}（#{{ item.lamp_id }}）</td>
            <td>{{ item.package_no }}</td>
            <td>{{ item.reported_at }}</td>
            <td>{{ item.restored_at }}</td>
            <td>{{ item.result }}</td>
            <td>{{ item.remark }}</td>
          </tr>
          <tr v-if="!history.length">
            <td colspan="6" class="empty-state">暂无复电历史</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条路灯照明记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>
type PackageRow = Row & {
  id: number
  package_no: string
  lamp_ids?: number[]
  arrival_order?: Array<{ order: number; vehicle_no: string }>
}

const ENDPOINT = '/api/lighting'
const columns = ["灯具编号", "灯具类型", "功率", "所属路段", "道路部位", "安装日期", "杆号", "回路", "供电区", "不亮原因", "设施状态"]
const filterFields = columns.slice(0, 3)

type Stat = { label: string; value: number | string }

const rows = ref<Row[]>([])
const total = ref(0)
const packages = ref<PackageRow[]>([])
const history = ref<Row[]>([])
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const stats = ref<Stat[]>([
  { label: '活动抢修包', value: 0 },
  { label: '台账灯具', value: 0 },
  { label: '不亮/抢修中', value: 0 },
  { label: '复电记录', value: 0 },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function formatList(value: unknown, separator: string): string {
  return Array.isArray(value) && value.length ? value.join(separator) : '—'
}

function formatLamps(value: unknown): string {
  return Array.isArray(value) ? value.map(String).join('、') : String(value ?? '—')
}

function formatArrival(value: unknown): string {
  if (!Array.isArray(value)) return '待派单'
  return value.length
    ? value.map((item) => `${String((item as Row).vehicle_no)}第${String((item as Row).order)}`).join('；')
    : '待派单'
}

function firstTwoLampIds(pkg: PackageRow): number[] {
  return (pkg.lamp_ids ?? []).slice(0, 2)
}

async function postJson(path: string, body: Record<string, unknown>, idempotencyKey?: string) {
  const headers: Record<string, string> = {}
  if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey
  const response = await request(path, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  })
  if (!response.ok) throw new Error(`接口返回 ${response.status}，抢修编排未生效`)
  return response.json() as Promise<Row>
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
      throw new Error(String(payload.message || '路灯照明动作未生效，请稍后重试'))
    }
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '路灯照明操作失败'
  }
}

async function dispatch(pkg: PackageRow, vehicleIds: number[], lineDiagramOk: boolean) {
  errorMessage.value = ''
  try {
    const payload = await postJson(
      `${ENDPOINT}/repair/packages/${pkg.id}/dispatch`,
      { values: { vehicle_ids: vehicleIds, line_diagram_ok: lineDiagramOk } },
      `dispatch-${pkg.id}-${Date.now()}`,
    )
    if (payload.ok === false) throw new Error(String(payload.message))
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '整包派单失败'
  }
}

async function restore(pkg: PackageRow, success: boolean, restoredLampIds: number[]) {
  errorMessage.value = ''
  try {
    const payload = await postJson(
      `${ENDPOINT}/repair/packages/${pkg.id}/restoration`,
      { values: { success, restored_lamp_ids: restoredLampIds, line_diagram_ok: true } },
      `restoration-${pkg.id}-${Date.now()}`,
    )
    if (payload.ok === false) throw new Error(String(payload.message))
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复电结果登记失败'
  }
}

async function recomputePackages() {
  errorMessage.value = ''
  try {
    const payload = await postJson(`${ENDPOINT}/repair/recompute`, {})
    if (payload.ok === false) throw new Error(String(payload.message))
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '增量重算失败'
  }
}

async function reloadLighting() {
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  const response = await request(`${ENDPOINT}?${query}`)
  if (!response.ok) throw new Error('路灯设施列表读取失败')
  const payload = await response.json() as Row
  rows.value = (payload.items as Row[]) ?? []
  total.value = Number(payload.total ?? rows.value.length)
}

async function reloadPackages() {
  const response = await request(`${ENDPOINT}/repair/packages?include_closed=true`)
  if (!response.ok) throw new Error('灭灯抢修包读取失败')
  const payload = await response.json() as Row
  packages.value = ((payload.items as PackageRow[]) ?? []).filter(
    (item) => item.stage !== '已取消',
  )
}

async function reloadHistory() {
  const response = await request(`${ENDPOINT}/repair/history`)
  if (!response.ok) throw new Error('复电历史读取失败')
  const payload = await response.json() as Row
  history.value = (payload.items as Row[]) ?? []
}

function updateStats() {
  stats.value = [
    { label: '活动抢修包', value: packages.value.length },
    { label: '台账灯具', value: total.value },
    {
      label: '不亮/抢修中',
      value: rows.value.filter((row) => row.status === '不亮' || row.status === '抢修中').length,
    },
    { label: '复电记录', value: history.value.length },
  ]
}

async function reloadAll() {
  errorMessage.value = ''
  try {
    await Promise.all([reloadLighting(), reloadPackages(), reloadHistory()])
    updateStats()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '路灯照明列表读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    await Promise.all([reloadLighting(), reloadPackages()])
    updateStats()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '路灯照明列表读取失败'
  }
}

onMounted(reloadAll)
</script>

<style scoped>
.repair-panel,
.history-panel {
  margin: 14px 0;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
}
.panel-head,
.package-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: flex-start;
}
.panel-head h3,
.history-panel h3 {
  margin: 0 0 4px;
}
.panel-head p {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
}
.repair-empty {
  padding: 20px;
}
.package-card {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px;
  margin-top: 10px;
  background: #fbfdff;
}
.stage-tag {
  display: inline-block;
  margin-left: 8px;
  padding: 2px 8px;
  border-radius: 999px;
  background: #eef4ff;
  color: var(--brand);
  font-size: 12px;
}
.stage-tag.safety {
  background: #fff4e5;
  color: #b54708;
}
.package-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  justify-content: flex-end;
}
.unified-fault,
.conclusion,
.cell-note {
  margin: 8px 0;
  font-size: 13px;
}
.conclusion,
.cell-note {
  color: var(--muted);
}
.route-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}
.route-grid div {
  border-top: 1px solid var(--border);
  padding-top: 6px;
}
.route-grid span {
  display: block;
  color: var(--muted);
  font-size: 12px;
  margin-bottom: 4px;
}
.compact th,
.compact td {
  padding: 6px 8px;
}
</style>
