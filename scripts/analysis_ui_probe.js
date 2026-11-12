/**
 * 分析页趋势图「数据表跟随窗口 + 折叠表惰性挂载」验收探针（playwright-core，无 test runner 依赖）。
 *
 * 对应 spec `.scratch/chart-table-window-perf/spec.md` §3 A1–A8（A9–A11 属后端，A12–A14 属工艺）。
 * 由 scripts/verify_analysis_ui.sh 调用；也可单独跑：
 *   BASE=http://127.0.0.1:3012 DAYS=730 bun scripts/analysis_ui_probe.js
 *
 * 语义前提（票 01/02 已落地）：
 * - 表行 = 窗口期数（`categories.slice(start)`），「全部」档即全量；
 * - caption 随窗口：「（近 N 期）」/「（全部账期）」；
 * - `tbody` 行惰性挂载：`<details>` 首次打开前 0 行，打开后保持挂载。
 *
 * 断言口径全部**由接口实测数据推导**（账期总数 / 首尾账期 / 默认窗口），不写死 730，
 * 因此 `--days 90` 等参数化运行同样成立。
 *
 * 输出：最后一行是 `PROBE_RESULT <json>`；断言项含 `id/name/pass/expected/actual`。
 * 退出码：0=全通过，1=有断言失败，2=环境/运行失败。
 * 依赖：frontend/node_modules/playwright-core + 本地已缓存的 chromium。
 */
const path = require('path')
const PW = require(path.join(__dirname, '..', 'frontend', 'node_modules', 'playwright-core'))

const BASE = process.env.BASE || 'http://127.0.0.1:3012'
/** 合成数据日粒度账期数（仅报告用；每档窗口的期望行数由接口返回推导）。 */
const FIXTURE_DAYS = Number(process.env.DAYS || process.env.EXPECT_ROWS || 730)
const FIXTURE_ITEMS = Number(process.env.ITEMS || 0)

const PERIOD_BUTTON = { day: '日', month: '月', quarter: '季度', year: '年', week: '周' }
/** 与 frontend/src/app/analysis/page.tsx:171-177 的 TREND_DEFAULT_WINDOW 一致。 */
const DEFAULT_WINDOW = { month: 24, quarter: 8, year: 2, week: 104, day: 730 }
/** 改前基线（.scratch/analysis-detail-prune/report.md §6，730 期 × 8 项）。 */
const BASELINE_2138 = 2138
const BASELINE_NODES = 10223

/** 图表 aria-label 里的窗口措辞（`窗口近 90 期`，带空格；TrendChart.tsx:234-236）。 */
const windowLabel = (w) => (w === 'all' ? '全部' : `近 ${w} 期`)
/** 窗口 chip 的按钮文案（`近90期`，无空格；page.tsx:163-168）。 */
const chipLabel = (w) => (w === 'all' ? '全部' : `近${w}期`)
const rowsFor = (count, w) => (w === 'all' ? count : Math.min(w, count))
/** TrendChart.table：`coversAll = window==='all' || visibleCount >= categories.length`。 */
const captionFor = (count, w) => (rowsFor(count, w) >= count ? '（全部账期）' : `（近 ${w} 期）`)

/** 页内取数工具（frame 靠「时间窗口」chip 组定位——只有趋势图有窗口）。 */
const PAGE_HELPERS = `
window.__pfProbe = {
  frame() {
    const frames = [...document.querySelectorAll('figure[data-chart-frame]')]
    return frames.find((f) => f.querySelector('[role="group"][aria-label="时间窗口"]')) || null
  },
  details() {
    const f = this.frame()
    return f ? f.querySelector('details') : null
  },
  rows() {
    const d = this.details()
    if (!d) return []
    return [...d.querySelectorAll('tbody tr')].map((tr) =>
      [...tr.children].map((td) => (td.textContent || '').replace(/\\s+/g, ' ').trim()),
    )
  },
  rowOf(period) {
    return this.rows().find((r) => r[0] === period) || null
  },
  state() {
    const f = this.frame()
    if (!f) return null
    const t = (el) => (el && el.textContent ? el.textContent : '').replace(/\\s+/g, ' ').trim()
    const d = f.querySelector('details')
    const win = f.querySelector('[role="group"][aria-label="时间窗口"]')
    const pressed = win ? [...win.querySelectorAll('button')].find((b) => b.getAttribute('aria-pressed') === 'true') : null
    const body = f.querySelector('[data-chart-body]')
    const rows = d ? this.rows() : []
    return {
      state: f.getAttribute('data-state'),
      hasDetails: !!d,
      open: d ? d.hasAttribute('open') : null,
      caption: d ? t(d.querySelector('caption')) : null,
      headers: d ? [...d.querySelectorAll('thead th')].map(t) : [],
      rowCount: rows.length,
      firstPeriod: rows.length ? rows[0][0] : null,
      lastPeriod: rows.length ? rows[rows.length - 1][0] : null,
      cellCount: rows.length ? rows[0].length : 0,
      summaryHeight: d ? Math.round(d.querySelector('summary').getBoundingClientRect().height) : null,
      pressedWindow: pressed ? t(pressed) : null,
      ariaLabel: body ? body.getAttribute('aria-label') : null,
      domNodes: document.querySelectorAll('*').length,
      bodyScrollHeight: document.body ? document.body.scrollHeight : null,
      docScrollHeight: document.documentElement.scrollHeight,
      docScrollWidth: document.documentElement.scrollWidth,
      docClientWidth: document.documentElement.clientWidth,
      looseTables: [...document.querySelectorAll('table')].filter((x) => !x.closest('details')).length,
    }
  },
}
`

const stable = (value) => JSON.stringify(value)
const same = (a, b) => stable(a) === stable(b)

;(async () => {
  const browser = await PW.chromium.launch({ headless: true })
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page = await ctx.newPage()
  await page.addInitScript({ content: PAGE_HELPERS })

  const checks = []
  /** 断言行：spec §3 的 A 编号 + 中文名 + expected/actual（机读）。 */
  const check = (id, name, pass, expected, actual) =>
    checks.push({ id, name, pass: !!pass, expected, actual })
  const skipped = []
  const result = { base: BASE, fixture: { days: FIXTURE_DAYS, items: FIXTURE_ITEMS }, checks, skipped }

  const errors = []
  page.on('pageerror', (e) => errors.push('pageerror: ' + String(e).slice(0, 150)))
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push('console: ' + m.text().slice(0, 150))
  })

  // mixed-chart 响应抓取：断言用的账期序列直接取自页面真实请求（不复制查询参数）。
  const responses = new Map()
  page.on('response', async (res) => {
    const url = res.url()
    if (!url.includes('/api/analysis/mixed-chart')) return
    try {
      const periodType = new URL(url).searchParams.get('period_type') || ''
      const json = await res.json()
      responses.set(periodType, {
        periodType,
        status: res.status(),
        count: Array.isArray(json.trend) ? json.trend.length : -1,
        periods: Array.isArray(json.trend) ? json.trend.map((t) => t.period) : [],
      })
    } catch {
      /* 响应体被页面消费或非 JSON：忽略，后续等待会报超时 */
    }
  })

  const frameLoc = page
    .locator('figure[data-chart-frame]')
    .filter({ has: page.locator('[role="group"][aria-label="时间窗口"]') })
    .first()
  const summaryLoc = frameLoc.locator('details > summary')
  const state = () => page.evaluate(() => window.__pfProbe.state())
  const rowOf = (period) => page.evaluate((p) => window.__pfProbe.rowOf(p), period)

  const waitRows = (expected, timeout = 60000) =>
    page.waitForFunction(
      (n) => {
        const d = window.__pfProbe.details()
        if (!d) return false
        return d.querySelectorAll('tbody tr').length === n
      },
      expected,
      { timeout },
    )

  const waitReady = (count, w, timeout = 180000) =>
    page.waitForFunction(
      ({ c, label }) => {
        const f = window.__pfProbe.frame()
        if (!f || f.getAttribute('data-state') !== 'ready') return false
        const body = f.querySelector('[data-chart-body]')
        const text = body ? body.getAttribute('aria-label') || '' : ''
        return text.includes(`${c} 个账期`) && text.includes(`窗口${label}`)
      },
      { c: count, label: windowLabel(w) },
      { timeout },
    )

  const waitResponse = async (periodType, timeout = 180000) => {
    const started = Date.now()
    while (Date.now() - started < timeout) {
      if (responses.has(periodType)) return responses.get(periodType)
      await page.waitForTimeout(100)
    }
    throw new Error(`等待 mixed-chart 响应超时：period_type=${periodType}`)
  }

  const clickPeriod = async (periodType) => {
    const label = PERIOD_BUTTON[periodType]
    await page.locator('button[aria-pressed]', { hasText: new RegExp(`^${label}$`) }).first().click()
  }

  const clickWindow = async (w) => {
    const label = chipLabel(w)
    await frameLoc
      .locator('[role="group"][aria-label="时间窗口"]')
      .locator('button')
      .filter({ hasText: new RegExp(`^${label}$`) })
      .first()
      .click()
  }

  const openTable = async () => {
    if (!(await page.evaluate(() => window.__pfProbe.details()?.hasAttribute('open')))) {
      await summaryLoc.click()
    }
    await page.waitForFunction(() => window.__pfProbe.details()?.hasAttribute('open') === true)
  }

  const closeTable = async () => {
    if (await page.evaluate(() => window.__pfProbe.details()?.hasAttribute('open'))) {
      await summaryLoc.click()
    }
    await page.waitForFunction(() => window.__pfProbe.details()?.hasAttribute('open') === false)
  }

  // ---------- 打开页面 ----------
  await page.goto(BASE + '/analysis', { waitUntil: 'domcontentloaded', timeout: 120000 })
  await page.waitForSelector('text=账期类型', { timeout: 120000 })

  const granularity = {}
  let activePeriod = 'month'

  // ---------- A8：四粒度回归（默认窗口下行数 = 该窗口期数；图/表账期首尾一致） ----------
  for (const periodType of ['month', 'quarter', 'year', 'week']) {
    if (periodType !== activePeriod) {
      await clickPeriod(periodType)
      activePeriod = periodType
    }
    const resp = await waitResponse(periodType)
    const count = resp.count
    const w = DEFAULT_WINDOW[periodType]
    const want = rowsFor(count, w)
    await waitReady(count, w)

    // 惰性挂载：切换粒度是重挂载（TrendChart key 含 periodType），未展开前必须是 0 行
    await waitRows(0)
    const collapsed = await state()
    await openTable()
    await waitRows(want)
    const expanded = await state()

    granularity[periodType] = {
      count,
      window: w,
      expectedRows: want,
      collapsedRows: collapsed.rowCount,
      rows: expanded.rowCount,
      firstPeriod: expanded.firstPeriod,
      lastPeriod: expanded.lastPeriod,
      expectedFirst: resp.periods[count - want],
      expectedLast: resp.periods[count - 1],
      caption: expanded.caption,
    }

    check(
      `A8-${periodType}-rows`,
      `${periodType} 默认窗口（近 ${w} 期）表行数 = ${want}`,
      expanded.rowCount === want,
      want,
      expanded.rowCount,
    )
    check(
      `A8-${periodType}-edges`,
      `${periodType} 表首尾账期与图首尾一致`,
      expanded.firstPeriod === resp.periods[count - want] && expanded.lastPeriod === resp.periods[count - 1],
      { first: resp.periods[count - want], last: resp.periods[count - 1] },
      { first: expanded.firstPeriod, last: expanded.lastPeriod },
    )
    check(
      `A8-${periodType}-caption`,
      `${periodType} caption 随窗口`,
      typeof expanded.caption === 'string' && expanded.caption.includes(captionFor(count, w)),
      `包含 ${captionFor(count, w)}`,
      expanded.caption,
    )
  }

  // ---------- 切到「日」：730 期折叠态（A4/A6 基线） ----------
  const dayT0 = Date.now()
  await clickPeriod('day')
  activePeriod = 'day'
  const dayResp = await waitResponse('day')
  const dayCount = dayResp.count
  result.dayCount = dayCount
  result.dayReadyMs = null
  await waitReady(dayCount, DEFAULT_WINDOW.day)
  await waitRows(0)
  result.dayReadyMs = Date.now() - dayT0

  const dayCollapsed730 = await state()

  check(
    'A4-a',
    `${dayCount} 期默认视图未展开时 tbody tr = 0（惰性挂载）`,
    dayCollapsed730.rowCount === 0,
    0,
    dayCollapsed730.rowCount,
  )
  check(
    'A3-730',
    `caption 随窗口（近 ${DEFAULT_WINDOW.day} 期）`,
    typeof dayCollapsed730.caption === 'string' && dayCollapsed730.caption.includes(captionFor(dayCount, DEFAULT_WINDOW.day)),
    `包含 ${captionFor(dayCount, DEFAULT_WINDOW.day)}`,
    dayCollapsed730.caption,
  )

  // 窗口切「近 90 期」（仍折叠）：A3 短窗文案 + A6 基线
  await clickWindow(90)
  await waitReady(dayCount, 90)
  await waitRows(0)
  const dayCollapsed90 = await state()
  check(
    'A3-90',
    'caption 随窗口（近 90 期）',
    typeof dayCollapsed90.caption === 'string' && dayCollapsed90.caption.includes(captionFor(dayCount, 90)),
    `包含 ${captionFor(dayCount, 90)}`,
    dayCollapsed90.caption,
  )
  check(
    'A4-b',
    '切窗口不改变未展开时的行数（仍为 0）',
    dayCollapsed90.rowCount === 0,
    0,
    dayCollapsed90.rowCount,
  )

  result.collapsed = {
    window730: { domNodes: dayCollapsed730.domNodes, bodyScrollHeight: dayCollapsed730.bodyScrollHeight },
    window90: { domNodes: dayCollapsed90.domNodes, bodyScrollHeight: dayCollapsed90.bodyScrollHeight },
  }
  check(
    'A6-dom',
    `折叠态 DOM 节点数 < 改前 730 期基线 ${BASELINE_NODES}`,
    dayCollapsed730.domNodes < BASELINE_NODES,
    `< ${BASELINE_NODES}`,
    dayCollapsed730.domNodes,
  )
  const heightDelta = Math.abs(dayCollapsed730.bodyScrollHeight - dayCollapsed90.bodyScrollHeight)
  check(
    'A6-height-window',
    '折叠态页面高度不随窗口期数变化（730 vs 90，±2px）',
    heightDelta <= 2,
    `|Δ| ≤ 2（730=${dayCollapsed730.bodyScrollHeight}, 90=${dayCollapsed90.bodyScrollHeight}）`,
    heightDelta,
  )
  const baselineDelta = Math.abs(dayCollapsed730.bodyScrollHeight - BASELINE_2138)
  result.scrollHeightVsBaseline = {
    baseline: BASELINE_2138,
    actual: dayCollapsed730.bodyScrollHeight,
    delta: baselineDelta,
  }
  check(
    'A6-height-baseline',
    `折叠态页面高度与改前基线 ${BASELINE_2138}px 相差 ≤ 2px`,
    baselineDelta <= 2,
    `|Δ| ≤ 2 vs ${BASELINE_2138}`,
    { actual: dayCollapsed730.bodyScrollHeight, delta: baselineDelta },
  )

  // ---------- 展开：A1/A2/A3 全量档 + A6 展开态对比 ----------
  await clickWindow(DEFAULT_WINDOW.day)
  await waitReady(dayCount, DEFAULT_WINDOW.day)
  await waitRows(0)
  const expandT0 = Date.now()
  await openTable()
  await waitRows(rowsFor(dayCount, DEFAULT_WINDOW.day))
  result.expandMs = Date.now() - expandT0
  const dayOpen730 = await state()

  check(
    'A1-730',
    `展开后表行数 = 窗口期数（近 ${DEFAULT_WINDOW.day} 期）`,
    dayOpen730.rowCount === rowsFor(dayCount, DEFAULT_WINDOW.day),
    rowsFor(dayCount, DEFAULT_WINDOW.day),
    dayOpen730.rowCount,
  )
  check(
    'A1-headers',
    '表头含「较上期变化额」「较上期变化%」',
    dayOpen730.headers.includes('较上期变化额') && dayOpen730.headers.includes('较上期变化%'),
    ['较上期变化额', '较上期变化%'],
    dayOpen730.headers,
  )
  check(
    'A3-all',
    'caption 随窗口（全部账期）',
    typeof dayOpen730.caption === 'string' && dayOpen730.caption.includes(captionFor(dayCount, DEFAULT_WINDOW.day)),
    `包含 ${captionFor(dayCount, DEFAULT_WINDOW.day)}`,
    dayOpen730.caption,
  )

  result.expanded = {
    window: DEFAULT_WINDOW.day,
    domNodes: dayOpen730.domNodes,
    bodyScrollHeight: dayOpen730.bodyScrollHeight,
    rowCount: dayOpen730.rowCount,
    heightDeltaVsCollapsed: dayOpen730.bodyScrollHeight - dayCollapsed730.bodyScrollHeight,
  }
  check(
    'A6-expand-height',
    '展开后页面高度增量 ≤ 300px（max-h-64 表体自滚，不把 730 行摊进页面）',
    result.expanded.heightDeltaVsCollapsed >= 0 && result.expanded.heightDeltaVsCollapsed <= 300,
    '0 ≤ Δ ≤ 300',
    result.expanded.heightDeltaVsCollapsed,
  )

  // A1 短窗：展开态切「近 90 期」→ 行数 = 90
  await clickWindow(90)
  await waitReady(dayCount, 90)
  await waitRows(rowsFor(dayCount, 90))
  const dayOpen90 = await state()
  const firstVisible90 = dayResp.periods[dayCount - rowsFor(dayCount, 90)]

  check(
    'A1-90',
    `展开后切「近 90 期」表行数 = ${rowsFor(dayCount, 90)}`,
    dayOpen90.rowCount === rowsFor(dayCount, 90),
    rowsFor(dayCount, 90),
    dayOpen90.rowCount,
  )
  check(
    'A2-first',
    '「近 90 期」视图首行账期 = 图第一个可见账期',
    dayOpen90.firstPeriod === firstVisible90,
    firstVisible90,
    dayOpen90.firstPeriod,
  )
  check(
    'A3-90-expanded',
    '「近 90 期」展开态 caption',
    typeof dayOpen90.caption === 'string' && dayOpen90.caption.includes(captionFor(dayCount, 90)),
    `包含 ${captionFor(dayCount, 90)}`,
    dayOpen90.caption,
  )

  // A2：同一账期在两档窗口下整行 JSON 全等
  const row90 = await rowOf(firstVisible90)
  await clickWindow('all')
  await waitReady(dayCount, 'all')
  await waitRows(dayCount)
  const dayOpenAll = await state()
  const rowAll = await rowOf(firstVisible90)

  check(
    'A2-row-equal',
    `同一账期（${firstVisible90}）在两档窗口下行内容全等`,
    row90 !== null && rowAll !== null && same(row90, rowAll),
    '两档窗口该行逐列 textContent 相同',
    { window90: row90, all: rowAll },
  )
  check(
    'A1-all',
    '「全部」档表行数 = 账期总数',
    dayOpenAll.rowCount === dayCount,
    dayCount,
    dayOpenAll.rowCount,
  )
  check(
    'A3-all-chip',
    '「全部」档 caption = 全部账期',
    typeof dayOpenAll.caption === 'string' && dayOpenAll.caption.includes(captionFor(dayCount, 'all')),
    `包含 ${captionFor(dayCount, 'all')}`,
    dayOpenAll.caption,
  )

  // ---------- A4：开合后行数不变 ----------
  await closeTable()
  const afterClose = await state()
  await openTable()
  await waitRows(dayCount)
  const afterReopen = await state()
  check(
    'A4-c',
    '关闭再展开后行数不变（首次打开后保持挂载）',
    afterClose.rowCount === dayCount && afterReopen.rowCount === dayCount,
    dayCount,
    { afterClose: afterClose.rowCount, afterReopen: afterReopen.rowCount },
  )

  // ---------- A7：390px 无横向溢出 ----------
  await page.setViewportSize({ width: 390, height: 844 })
  await page.waitForTimeout(400)
  await closeTable()
  const m390collapsed = await state()
  result.mobile = { collapsed: m390collapsed, }
  check(
    'A7',
    '390px 折叠态无横向溢出（scrollWidth === clientWidth）',
    m390collapsed.docScrollWidth === m390collapsed.docClientWidth,
    'scrollWidth === clientWidth',
    { scrollWidth: m390collapsed.docScrollWidth, clientWidth: m390collapsed.docClientWidth },
  )
  check(
    'retained-summary-44',
    'summary 命中高度 ≥ 44px（390px）',
    m390collapsed.summaryHeight >= 44,
    '>= 44',
    m390collapsed.summaryHeight,
  )
  await openTable()
  await waitRows(dayCount)
  const m390open = await state()
  result.mobile.expanded = {
    rowCount: m390open.rowCount,
    docScrollWidth: m390open.docScrollWidth,
    docClientWidth: m390open.docClientWidth,
  }
  check(
    'A7-open',
    '390px 展开态无整页横向溢出',
    m390open.docScrollWidth === m390open.docClientWidth,
    'scrollWidth === clientWidth',
    { scrollWidth: m390open.docScrollWidth, clientWidth: m390open.docClientWidth },
  )

  // ---------- A5：命令式开表 ----------
  // 「查看数据表」按钮只在 ChartFrame 的 insufficient 态出现（ChartFrame.tsx:365-382），
  // 而分析页 trendState 只有 error/loading/ready/empty（page.tsx:749-753），该态在本仓无生产者，
  // 故 UI 不可达；改为覆盖其 DOM 副作用等价路径：外部置 `details.open = true`（不走 React 状态，
  // 与 openTable() 的 `element.open = true` 同一路径），要求行随之渲染。
  skipped.push({
    id: 'A5',
    name: 'header/状态区「查看数据表」按钮 → 命令式开表',
    reason:
      'insufficient 态在全仓无生产者（仅 ChartFrame.tsx:365 定义，分析页 trendState 不含该值），按钮不可达；改测下方 A5-imperative 等价路径',
  })

  await page.setViewportSize({ width: 1440, height: 900 })
  await clickPeriod('month') // 换粒度 = 重挂载，回到未打开状态
  activePeriod = 'month'
  await waitResponse('month')
  await waitReady((await waitResponse('month')).count, DEFAULT_WINDOW.month)
  await waitRows(0)
  await clickPeriod('day')
  await waitResponse('day')
  await waitReady(dayCount, DEFAULT_WINDOW.day)
  await waitRows(0)
  const beforeImperative = await state()
  await page.evaluate(() => {
    window.__pfProbe.details().open = true
  })
  await waitRows(rowsFor(dayCount, DEFAULT_WINDOW.day))
  const afterImperative = await state()
  check(
    'A5-imperative',
    '外部置 details.open = true（openTable 的 DOM 副作用等价路径）后行已渲染',
    beforeImperative.rowCount === 0 && afterImperative.rowCount === rowsFor(dayCount, DEFAULT_WINDOW.day),
    { before: 0, after: rowsFor(dayCount, DEFAULT_WINDOW.day) },
    { before: beforeImperative.rowCount, after: afterImperative.rowCount },
  )
  await closeTable()
  const afterImperativeClose = await state()
  check(
    'A5-imperative-kept',
    '命令式打开后再关闭，行保持挂载（不回落）',
    afterImperativeClose.rowCount === rowsFor(dayCount, DEFAULT_WINDOW.day),
    rowsFor(dayCount, DEFAULT_WINDOW.day),
    afterImperativeClose.rowCount,
  )

  // ---------- 保留断言 ----------
  const finalState = await state()
  check(
    'retained-details',
    '数据表存在于折叠 details 内',
    finalState.hasDetails === true,
    true,
    finalState.hasDetails,
  )
  check(
    'retained-loose-tables',
    '页面级独立表格数量为 0（无冗余数据明细卡）',
    finalState.looseTables === 0,
    0,
    finalState.looseTables,
  )
  result.errors = errors.slice(0, 5)
  result.errorCount = errors.length
  check(
    'retained-errors',
    '运行期无 pageerror / console.error',
    errors.length === 0,
    0,
    errors.slice(0, 3),
  )

  result.granularity = granularity
  const failed = checks.filter((c) => !c.pass)
  result.passed = checks.length - failed.length
  result.failed = failed.length
  console.log('PROBE_RESULT ' + JSON.stringify(result))
  await browser.close()
  process.exit(failed.length === 0 ? 0 : 1)
})().catch((e) => {
  console.error('FATAL', e)
  process.exit(2)
})