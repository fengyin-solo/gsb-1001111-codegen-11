<template>
  <section class="page" data-module="lighting_repair">
    <header class="page-head">
      <div>
        <h2>灭灯区域聚合与抢修编排</h2>
        <p class="page-desc">相邻杆号、同回路、同供电区的故障收拢为一个抢修包；查看巡查路线与车辆到达顺序，按包派单，不按单灯重复派单。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openReport">上报灭灯故障</button>
        <button class="btn" type="button" @click="rebuild">增量重算聚合</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>抢修阶段</span>
        <select v-model="stageFilter">
          <option value="">全部阶段</option>
          <option v-for="stage in stages" :key="stage" :value="stage">{{ stage }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
    </form>

    <div v-if="errorMessage" class="error-banner">{{ errorMessage }}</div>

    <article v-for="pkg in packages" :key="pkg.抢修包号" class="package-card">
      <header class="package-head">
        <div class="package-title">
          <strong>{{ pkg.抢修包号 }}</strong>
          <span class="stage-tag" :class="stageClass(pkg.阶段)">{{ pkg.阶段 }}</span>
          <span class="level-tag" v-if="pkg.影响级别">通行影响：{{ pkg.影响级别 }}</span>
        </div>
        <div class="package-meta">
          <span>供电区：{{ pkg.供电区 }}</span>
          <span>回路：{{ (pkg.回路集合 || []).join('、') || '—' }}</span>
          <span>杆号范围：{{ pkg.杆号范围 }}</span>
          <span>故障灯：{{ pkg.灯具数量 }} 盏</span>
          <span>路段：{{ pkg.所属路段 }}</span>
          <span>出车：{{ pkg.车牌号 || '未派车' }}</span>
          <span>线路图：{{ pkg.线路图版本 }}</span>
        </div>
      </header>

      <div class="route-grid">
        <div class="route-col">
          <h4>巡查路线（按杆号推进）</h4>
          <ol class="route-list">
            <li v-for="stop in pkg.巡查路线" :key="stop.灯具编号">
              <em>{{ stop.顺序 }}</em>
              <span>{{ stop.杆号 }} · {{ stop.灯具编号 }} · {{ stop.道路位置 }}（回路 {{ stop.回路 }}）</span>
            </li>
          </ol>
        </div>
        <div class="route-col">
          <h4>车辆到达顺序（安全级别优先）</h4>
          <ol class="route-list">
            <li v-for="stop in pkg.车辆到达顺序" :key="stop.顺序">
              <em>{{ stop.顺序 }}</em>
              <span>先到{{ stop.级别 }} · 起始 {{ stop.起始杆号 }}（{{ stop.供电区 }}）</span>
            </li>
          </ol>
        </div>
      </div>

      <details class="stage-log">
        <summary>阶段流转记录（{{ (pkg.阶段记录 || []).length }} 步）</summary>
        <ul>
          <li v-for="(log, idx) in pkg.阶段记录" :key="idx">
            <span class="log-time">{{ log.时间 }}</span>
            <span class="log-stage">{{ log.阶段 }}</span>
            <span>{{ log.说明 }}</span>
          </li>
        </ul>
      </details>

      <footer class="package-actions">
        <template v-if="pkg.阶段 === '待编制'">
          <button class="btn primary" type="button" @click="updateDiagram(pkg, true)">线路图更新成功</button>
          <button class="btn danger" type="button" @click="updateDiagram(pkg, false)">模拟更新失败（回滚）</button>
        </template>
        <template v-else-if="pkg.阶段 === '线路图更新中'">
          <button class="btn primary" type="button" @click="updateDiagram(pkg, true)">确认更新成功</button>
          <button class="btn danger" type="button" @click="updateDiagram(pkg, false)">更新失败（回滚整包）</button>
        </template>
        <template v-else-if="pkg.阶段 === '待派单'">
          <button class="btn primary" type="button" @click="openDispatch(pkg)">派单抢修</button>
        </template>
        <template v-else-if="pkg.阶段 === '现场抢修中'">
          <button class="btn primary" type="button" @click="openRestore(pkg)">复电确认</button>
        </template>
        <span v-else class="done-text">已复电归档，历史记录保留在灯具台账</span>
      </footer>
    </article>

    <p v-if="!packages.length" class="empty-state">当前没有灭灯抢修包，可先上报故障或等待巡查聚合。</p>

    <!-- 故障上报 -->
    <div v-if="reportOpen" class="modal-mask" @click.self="reportOpen = false">
      <form class="modal" @submit.prevent="submitReport">
        <h3>上报灭灯故障</h3>
        <p class="modal-tip">并发重复上报请使用同一幂等键，后端只受理一次。</p>
        <label v-for="field in reportFields" :key="field.key" class="modal-field">
          <span>{{ field.label }}</span>
          <input v-model="reportForm[field.key]" :placeholder="field.placeholder" />
        </label>
        <label class="modal-field">
          <span>道路位置</span>
          <select v-model="reportForm.道路位置">
            <option>主路</option>
            <option>匝道</option>
            <option>辅道</option>
          </select>
        </label>
        <footer class="modal-actions">
          <button type="button" class="btn" @click="reportOpen = false">取消</button>
          <button type="submit" class="btn primary">提交并聚合</button>
        </footer>
      </form>
    </div>

    <!-- 派单 -->
    <div v-if="dispatchTarget" class="modal-mask" @click.self="dispatchTarget = null">
      <form class="modal" @submit.prevent="submitDispatch">
        <h3>派单抢修 · {{ dispatchTarget.抢修包号 }}</h3>
        <p class="modal-tip">整包派给一辆车，校验结论同步到灯具台账、巡查清单与车辆任务汇总。</p>
        <label class="modal-field">
          <span>车牌号</span>
          <input v-model="dispatchForm.车牌号" list="plate-options" placeholder="如 养护车辆样例1" />
          <datalist id="plate-options">
            <option v-for="plate in plates" :key="plate" :value="plate" />
          </datalist>
        </label>
        <label class="modal-field">
          <span>巡查人员</span>
          <input v-model="dispatchForm.巡查人员" placeholder="如 张三" />
        </label>
        <footer class="modal-actions">
          <button type="button" class="btn" @click="dispatchTarget = null">取消</button>
          <button type="submit" class="btn primary">确认派单</button>
        </footer>
      </form>
    </div>

    <!-- 复电 -->
    <div v-if="restoreTarget" class="modal-mask" @click.self="restoreTarget = null">
      <form class="modal" @submit.prevent="submitRestore">
        <h3>复电确认 · {{ restoreTarget.抢修包号 }}</h3>
        <p class="modal-tip">本次复电结果会逐灯记入历史不亮记录，不覆盖历史。</p>
        <label class="modal-field">
          <span>复电结果</span>
          <select v-model="restoreForm.复电结果">
            <option>全部复电</option>
            <option>部分复电，遗留匝道灯待件</option>
            <option>更换电缆后复电</option>
            <option>更换光源后复电</option>
          </select>
        </label>
        <footer class="modal-actions">
          <button type="button" class="btn" @click="restoreTarget = null">取消</button>
          <button type="submit" class="btn primary">确认复电归档</button>
        </footer>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Pkg = Record<string, any>

const ENDPOINT = '/api/lighting-repair'
const stages = ['待编制', '线路图更新中', '待派单', '现场抢修中', '复电归档']

const packages = ref<Pkg[]>([])
const stageFilter = ref('')
const errorMessage = ref('')
const plates = ref<string[]>([])

const reportOpen = ref(false)
const dispatchTarget = ref<Pkg | null>(null)
const restoreTarget = ref<Pkg | null>(null)

const reportFields = [
  { key: '灯具编号', label: '灯具编号', placeholder: '如 LIGH-0007' },
  { key: '所属路段', label: '所属路段', placeholder: '如 北二环主路' },
  { key: '杆号', label: '杆号', placeholder: '如 BD-K012+400-17' },
  { key: '回路', label: '回路', placeholder: '如 WL-甲' },
  { key: '供电区', label: '供电区', placeholder: '如 北二环1号箱变' },
  { key: '不亮原因', label: '不亮原因', placeholder: '如 电缆挖断' },
]
const reportForm = reactive<Record<string, string>>({
  灯具编号: '', 所属路段: '', 杆号: '', 回路: '', 供电区: '', 不亮原因: '', 道路位置: '主路',
})
const dispatchForm = reactive({ 车牌号: '养护车辆样例1', 巡查人员: '' })
const restoreForm = reactive({ 复电结果: '全部复电' })

const stats = ref([
  { label: '抢修包总数', value: 0 },
  { label: '待编制', value: 0 },
  { label: '抢修中', value: 0 },
  { label: '已复电归档', value: 0 },
])

function stageClass(stage: string) {
  return {
    待编制: 'tag-gray',
    线路图更新中: 'tag-blue',
    待派单: 'tag-orange',
    现场抢修中: 'tag-red',
    复电归档: 'tag-green',
  }[stage] || 'tag-gray'
}

async function callApi(path: string, body?: unknown, idempotent = false) {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (idempotent) headers['Idempotency-Key'] = `FE-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body ?? {}),
    headers,
  })
  const payload = await response.json()
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.detail || payload.message || '操作未生效')
  }
  return payload
}

async function reload() {
  errorMessage.value = ''
  try {
    const query = stageFilter.value ? `?stage=${encodeURIComponent(stageFilter.value)}` : ''
    const response = await request(`${ENDPOINT}/packages${query}`)
    if (!response.ok) throw new Error('抢修包列表读取失败')
    const payload = await response.json()
    packages.value = payload.items ?? []
    stats.value[0].value = payload.total ?? 0
    stats.value[1].value = packages.value.filter((p) => ['待编制', '线路图更新中', '待派单'].includes(p.阶段)).length
    stats.value[2].value = packages.value.filter((p) => p.阶段 === '现场抢修中').length
    stats.value[3].value = packages.value.filter((p) => p.阶段 === '复电归档').length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '抢修包列表读取失败'
  }
}

async function loadPlates() {
  try {
    const response = await request('/api/vehicle?size=200')
    if (!response.ok) return
    const payload = await response.json()
    plates.value = (payload.items ?? []).map((row: Record<string, string>) => row.车牌号).filter(Boolean)
  } catch {
    /* 车牌候选仅为便利输入，失败不阻塞 */
  }
}

function openReport() {
  reportOpen.value = true
}

async function submitReport() {
  try {
    const values = { ...reportForm }
    await callApi(`${ENDPOINT}/faults`, { values }, true)
    reportOpen.value = false
    Object.keys(reportForm).forEach((key) => { reportForm[key] = key === '道路位置' ? '主路' : '' })
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '故障上报失败'
  }
}

async function updateDiagram(pkg: Pkg, success: boolean) {
  try {
    await callApi(`${ENDPOINT}/packages/${pkg.id}/diagram`, { 成功: success })
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '线路图更新失败'
  }
}

function openDispatch(pkg: Pkg) {
  dispatchTarget.value = pkg
}

async function submitDispatch() {
  if (!dispatchTarget.value) return
  try {
    await callApi(`${ENDPOINT}/packages/${dispatchTarget.value.id}/dispatch`, { ...dispatchForm })
    dispatchTarget.value = null
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '派单失败'
  }
}

function openRestore(pkg: Pkg) {
  restoreTarget.value = pkg
}

async function submitRestore() {
  if (!restoreTarget.value) return
  try {
    await callApi(`${ENDPOINT}/packages/${restoreTarget.value.id}/restore`, { ...restoreForm })
    restoreTarget.value = null
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复电确认失败'
  }
}

async function rebuild() {
  try {
    const payload = await callApi(`${ENDPOINT}/rebuild`)
    errorMessage.value = ''
    await reload()
    window.alert(payload.message)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '增量重算失败'
  }
}

onMounted(() => {
  void reload()
  void loadPlates()
})
</script>

<style scoped>
.error-banner {
  background: #fef3f2;
  border: 1px solid #fecdca;
  color: #b42318;
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 13px;
  margin-bottom: 12px;
}
.package-card {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
  margin-bottom: 14px;
}
.package-head { border-bottom: 1px dashed var(--border); padding-bottom: 10px; }
.package-title { display: flex; align-items: center; gap: 10px; }
.package-title strong { font-size: 15px; }
.stage-tag, .level-tag {
  font-size: 12px;
  border-radius: 999px;
  padding: 2px 10px;
  border: 1px solid transparent;
}
.tag-gray { background: #f1f5f9; color: #475569; border-color: #e2e8f0; }
.tag-blue { background: #eff6ff; color: #1d4ed8; border-color: #bfdbfe; }
.tag-orange { background: #fff7ed; color: #c2410c; border-color: #fed7aa; }
.tag-red { background: #fef2f2; color: #b91c1c; border-color: #fecaca; }
.tag-green { background: #ecfdf5; color: #047857; border-color: #a7f3d0; }
.level-tag { background: #eef2ff; color: #4338ca; border-color: #c7d2fe; }
.package-meta { display: flex; flex-wrap: wrap; gap: 6px 18px; margin-top: 8px; font-size: 12px; color: var(--muted); }
.route-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; padding: 10px 0; }
.route-col h4 { margin: 0 0 6px; font-size: 13px; }
.route-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.route-list li { display: flex; gap: 8px; align-items: center; font-size: 13px; }
.route-list em {
  width: 20px; height: 20px; border-radius: 50%;
  background: var(--brand); color: #fff;
  font-style: normal; font-size: 12px;
  display: inline-flex; align-items: center; justify-content: center;
  flex: none;
}
.stage-log { font-size: 12px; color: var(--muted); border-top: 1px dashed var(--border); padding-top: 8px; }
.stage-log ul { list-style: none; margin: 6px 0 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.log-time { margin-right: 8px; }
.log-stage { display: inline-block; min-width: 84px; color: #1f2937; }
.package-actions { display: flex; gap: 8px; margin-top: 10px; align-items: center; }
.btn.danger { border-color: #fecdca; color: #b42318; }
.done-text { font-size: 13px; color: #047857; }
.modal-mask {
  position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45);
  display: flex; align-items: center; justify-content: center; z-index: 20;
}
.modal {
  background: #fff; border-radius: 10px; padding: 18px 20px;
  width: 420px; max-width: 92vw; display: flex; flex-direction: column; gap: 10px;
}
.modal h3 { margin: 0; }
.modal-tip { margin: 0; font-size: 12px; color: var(--muted); }
.modal-field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.modal-field input, .modal-field select {
  border: 1px solid var(--border); border-radius: 6px; padding: 7px 9px; font-size: 13px; color: #1f2937;
}
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 6px; }
@media (max-width: 900px) { .route-grid { grid-template-columns: 1fr; } }
</style>
