/**
 * 记录明细「默认按更新时间排序 + 更新时间列」浏览器验收探针（playwright-core，无 test runner 依赖）。
 *
 * 对应 spec `.scratch/records-updated-at/spec.md` §3 第 5–7 条（首屏请求参数 / 最右列渲染与
 * title 原串 / 点表头翻转 / 移动端 meta / 当前值视图不受影响）——把「读码推断」换成可复跑的抓包 + 真机截图。
 * 由 scripts/verify_records_updated_at.sh 调用；也可单独跑（需已起前后端）：
 *   BASE=http://127.0.0.1:3110 SHOT_DIR=/tmp/shots bun scripts/records_sort_probe.js
 *
 * 数据前提（合成，绝不用真库）：fixture 由 scripts/make_analysis_long_fixture.py 生成，
 * 再对副本把 30 行刷成两组同刻时间戳（5 行较新 + 25 行较旧，各行带 `示例-平局-` 前缀），
 * 于是平局组正好落在 `updated_at DESC` 的头部、且横跨 page_size=10 的三页，翻页稳定性才有得测。
 *
 * 断言口径尽量**由接口实测数据推导**（首行 title 比对首个 /api/assets 响应里的 updated_at 原串），
 * 不写死时间戳——除非环境显式给了 TIE_NEW / TIE_OLD（.sh 会传，用于确认 fixture 已生效）。
 *
 * 输出：最后一行是 `PROBE_RESULT <json>`；断言项含 `id/name/pass/expected/actual`。
 * 退出码：0=全通过，1=有断言失败，2=环境/运行失败。
 * 依赖：frontend/node_modules/playwright-core + 本地已缓存的 chromium（headless）。
 */
const path = require('path')
const fs = require('fs')
const PW = require(path.join(__dirname, '..', 'frontend', 'node_modules', 'playwright-core'))

const BASE = process.env.BASE || 'http://127.0.0.1:3110'
const SHOT_DIR = process.env.SHOT_DIR || ''
/** 桌面视口（业主裁决的横向滚动记录口径）。 */
const DESKTOP = { width: 1536, height: 900 }
/** 移动视口（<768 走卡片分支）。 */
const MOBILE = { width: 390, height: 844 }
/** 平局行数（fixture 生成口径，仅用于报告）。 */
const TIE_COUNT = Number(process.env.TIE_COUNT || 30)
const TIE_NEW = process.env.TIE_NEW || ''
const TIE_OLD = process.env.TIE_OLD || ''

const TABLE_LABEL = '资产记录列表'
/** 移动端 meta 值的时间口径（format.ts:formatDateTime → `YYYY/MM/DD HH:mm`）。 */
const CELL_TEXT_RE = /^\d{4}\/\d{2}\/\d{2} \d{2}:\d{2}$/
/** 后端原串口径（routes/assets.py:format_date → datetime.isoformat()：T 分隔 + 6 位小数秒）。 */
const RAW_TITLE_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}$/

/**
 * 页内取数工具。
 *
 * 说明：桌面表头最后一列是 rowActions 的「操作」（DataTable.tsx:300-308），
 * 所以「更新时间」不是字面意义的最后一个 th；探针按**表头文本**定位列下标，
 * 再用同一列下标取首行单元格（列顺序与列定义一一对应，选择列在最前）。
 */
const PAGE_HELPERS = `
window.__pfProbe = {
  table() {
    return document.querySelector('table[aria-label="${TABLE_LABEL}"]')
  },
  headers() {
    const t = this.table()
    if (!t) return []
    return [...t.querySelectorAll('thead th')].map((th) => ({
      text: (th.textContent || '').replace(/\\s+/g, ' ').trim(),
      ariaSort: th.getAttribute('aria-sort'),
      hasSortIcon: !!th.querySelector('svg'),
      right: Math.round(th.getBoundingClientRect().right * 10) / 10,
    }))
  },
  updatedCol() {
    return this.headers().findIndex((h) => h.text === '更新时间')
  },
  firstRow() {
    const t = this.table()
    if (!t) return null
    const idx = this.updatedCol()
    const tr = t.querySelector('tbody tr')
    if (!tr || idx < 0) return null
    const td = tr.children[idx]
    if (!td) return null
    const holder = td.querySelector('span[title]')
    const r = td.getBoundingClientRect()
    return {
      text: (td.textContent || '').replace(/\\s+/g, ' ').trim(),
      title: holder ? holder.getAttribute('title') : null,
      right: Math.round(r.right * 10) / 10,
      rowCount: t.querySelectorAll('tbody tr').length,
    }
  },
  scrollBox() {
    const t = this.table()
    if (!t || !t.parentElement) return null
    const box = t.parentElement
    return {
      scrollWidth: box.scrollWidth,
      clientWidth: box.clientWidth,
      scrollLeft: box.scrollLeft,
      overflowX: getComputedStyle(box).overflowX,
      hasHScroll: box.scrollWidth > box.clientWidth + 1,
    }
  },
  cardCount() {
    return document.querySelectorAll('ul[aria-label="${TABLE_LABEL}"] > li').length
  },
  card(i) {
    const li = document.querySelectorAll('ul[aria-label="${TABLE_LABEL}"] > li')[i]
    if (!li) return null
    const dts = [...li.querySelectorAll('dt')]
    const dt = dts.find((d) => (d.textContent || '').trim() === '更新时间')
    const dd = dt ? dt.nextElementSibling : null
    const holder = dt ? dt.parentElement : null
    return {
      labels: dts.map((d) => (d.textContent || '').trim()),
      value: dd ? (dd.textContent || '').replace(/\\s+/g, ' ').trim() : null,
      valueTitle: dd && dd.querySelector('[title]') ? dd.querySelector('[title]').getAttribute('title') : null,
      dtCount: dts.length,
      wrapperClass: holder ? String(holder.className || '') : null,
      scrollWidth: li.scrollWidth,
      clientWidth: li.clientWidth,
    }
  },
  docWidth() {
    return { scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth }
  },
}
`

/**
 * 挂 `/api/assets` 响应的记录器。
 * 只认**精确路径** `/api/assets`：`/api/assets/current`（当前值视图）与 `/api/assets/:id` 都不算，
 * 否则 A1「首屏请求」与 A7「当前值视图不受影响」会互相污染。
 */
function attachRecorder(page) {
  const list = []
  page.on('response', (res) => {
    let url
    try {
      url = new URL(res.url())
    } catch {
      return
    }
    if (url.pathname !== '/api/assets') return
    const entry = {
      url: res.url(),
      status: res.status(),
      page: url.searchParams.get('page'),
      pageSize: url.searchParams.get('page_size'),
      sortField: url.searchParams.get('sort_field'),
      sortOrder: url.searchParams.get('sort_order'),
      items: null,
      total: null,
    }
    list.push(entry)
    res
      .json()
      .then((json) => {
        entry.items = (json.items || []).map((i) => ({
          id: i.id,
          updated_at: i.updated_at,
          asset_name: i.asset_name,
        }))
        entry.total = json.total
      })
      .catch(() => {
        /* 非 JSON / 被页面消费：留 items=null，等待方会跳过 */
      })
  })
  return list
}

/** 轮询等待（不依赖 expect 库）。 */
async function waitFor(page, fn, timeout = 60000, label = '条件') {
  const t0 = Date.now()
  while (Date.now() - t0 < timeout) {
    const v = fn()
    if (v) return v
    await page.waitForTimeout(100)
  }
  throw new Error(`等待${label}超时（${timeout}ms）`)
}

/**
 * 等**从 from 起**出现的第一条满足 pred 的、已解析完的 /api/assets 响应。
 * 用「从下标扫描」而不是「等第 n 条」：页面可能多发/漏发请求（React Query 缓存命中就不发），
 * 按固定下标等会把「缓存命中、未发请求」误判成超时。
 */
const waitAssetMatching = (page, list, from, pred = () => true, timeout = 60000) =>
  waitFor(
    page,
    () => list.slice(from).find((e) => e.items !== null && pred(e)) || null,
    timeout,
    `第 ${from + 1} 条起的 /api/assets 响应（${pred.toString().slice(0, 60)}）`,
  )

const stable = (v) => JSON.stringify(v)

;(async () => {
  const browser = await PW.chromium.launch({ headless: true })

  const checks = []
  const notes = []
  const screenshots = []
  const metrics = {}
  /** 断言行：spec §3 的 A 编号 + 中文名 + expected/actual（机读）。 */
  const check = (id, name, pass, expected, actual) =>
    checks.push({ id, name, pass: !!pass, expected, actual })

  if (SHOT_DIR) fs.mkdirSync(SHOT_DIR, { recursive: true })
  const shot = async (page, name) => {
    if (!SHOT_DIR) return
    const file = path.join(SHOT_DIR, name)
    await page.screenshot({ path: file })
    screenshots.push(file)
  }

  const result = {
    base: BASE,
    fixture: { tieCount: TIE_COUNT, tieNew: TIE_NEW || null, tieOld: TIE_OLD || null },
    viewports: { desktop: DESKTOP, mobile: MOBILE },
    checks,
    notes,
    screenshots,
  }

  // =====================================================================
  // 桌面 1536×900：A1 / A2 / A3 / A5 → A8（翻页，仍在默认 desc 首屏） → A4（点表头翻 asc）
  // =====================================================================
  const ctxDesktop = await browser.newContext({ viewport: DESKTOP })
  const page = await ctxDesktop.newPage()
  await page.addInitScript({ content: PAGE_HELPERS })
  const desktopAssets = attachRecorder(page)

  const errors = []
  page.on('pageerror', (e) => errors.push('pageerror: ' + String(e).slice(0, 150)))
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push('console: ' + m.text().slice(0, 150))
  })

  await page.goto(BASE + '/records?view=ledger', { waitUntil: 'domcontentloaded', timeout: 120000 })

  // ---------- A1：首屏请求带 sort_field=updated_at + sort_order=desc ----------
  const first = await waitAssetMatching(page, desktopAssets, 0)
  check(
    'A1',
    '/records?view=ledger 首屏 GET /api/assets query 含 sort_field=updated_at 且 sort_order=desc',
    first.sortField === 'updated_at' && first.sortOrder === 'desc',
    { sort_field: 'updated_at', sort_order: 'desc' },
    { sort_field: first.sortField, sort_order: first.sortOrder, page: first.page, page_size: first.pageSize, url: first.url },
  )

  await page.waitForFunction(() => window.__pfProbe.firstRow() !== null, null, { timeout: 60000 })
  await page.waitForTimeout(300)
  await shot(page, 'ledger-desktop-1536.png')

  // ---------- A2：表头文字 + aria-sort ----------
  const headers = await page.evaluate(() => window.__pfProbe.headers())
  const updIdx = await page.evaluate(() => window.__pfProbe.updatedCol())
  const lastHeader = headers.length ? headers[headers.length - 1] : null
  const updHeader = updIdx >= 0 ? headers[updIdx] : null
  metrics.headers = headers.map((h) => `${h.text}${h.ariaSort ? `[${h.ariaSort}]` : ''}`)
  check(
    'A2-text',
    '桌面表头存在「更新时间」列，且是最后一个数据列（其后只允许 rowActions 的「操作」）',
    updHeader !== null && (updIdx === headers.length - 1 || (updIdx === headers.length - 2 && lastHeader.text === '操作')),
    '「更新时间」为最后一个数据列（其后可跟「操作」）',
    { index: updIdx, headerCount: headers.length, header: updHeader && updHeader.text, lastHeader: lastHeader && lastHeader.text },
  )
  check(
    'A2-aria',
    '「更新时间」表头 aria-sort="descending"（默认按更新时间倒序）',
    updHeader !== null && updHeader.ariaSort === 'descending',
    'descending',
    { ariaSort: updHeader && updHeader.ariaSort, hasSortIcon: updHeader && updHeader.hasSortIcon },
  )

  // ---------- A3：首行单元格文本 + title 原串 ----------
  const row1 = await page.evaluate(() => window.__pfProbe.firstRow())
  const apiFirst = first.items && first.items[0]
  metrics.firstRow = row1
  check(
    'A3-text',
    '首行「更新时间」单元格文本匹配 YYYY/MM/DD HH:mm',
    !!row1 && CELL_TEXT_RE.test(row1.text),
    String(CELL_TEXT_RE),
    row1 && row1.text,
  )
  check(
    'A3-title',
    '首行单元格 title = 后端原串（T 分隔 + 6 位小数秒）',
    !!row1 && !!apiFirst && row1.title === apiFirst.updated_at && RAW_TITLE_RE.test(String(row1.title)),
    { equalsResponse: apiFirst && apiFirst.updated_at, matchesRegex: String(RAW_TITLE_RE) },
    { title: row1 && row1.title, responseUpdatedAt: apiFirst && apiFirst.updated_at },
  )
  if (TIE_NEW) {
    check(
      'A3-fixture-head',
      '首行即 fixture 的「较新」平局组（确认 30 行平局落在倒序头部）',
      !!apiFirst && apiFirst.updated_at === TIE_NEW,
      TIE_NEW,
      apiFirst && apiFirst.updated_at,
    )
  }

  // ---------- A5：列完整可达 + 横向滚动实测（只记录不判失败） ----------
  // 表宽 1364→1836（spec D4，业主 2026-11-11 裁决接受容器内横滚）后，「更新时间」列右缘
  // 必然超出 1536 视口，旧断言「right ≤ 视口宽」与新规格冲突。改为验「不超出表格内容宽」
  // —— 列的**完整性/可横滚到达**；屏内可见性由 table-width 的 A4 零截断判据覆盖。
  const scrollBox = await page.evaluate(() => window.__pfProbe.scrollBox())
  const thRight = updHeader ? updHeader.right : null
  const tdRight = row1 ? row1.right : null
  metrics.a5 = { viewport: DESKTOP, thRight, tdRight, scrollBox }
  check(
    'A5-visible',
    '「更新时间」表头与首行单元格右边缘不超出表格内容宽度（right ≤ scrollWidth + 1，即未被裁切、可横滚到达）',
    typeof thRight === 'number' && typeof tdRight === 'number'
      && !!scrollBox && typeof scrollBox.scrollWidth === 'number'
      && thRight <= scrollBox.scrollWidth + 1 && tdRight <= scrollBox.scrollWidth + 1,
    'right ≤ 表格 scrollWidth + 1',
    { thRight, tdRight, scrollWidth: scrollBox && scrollBox.scrollWidth },
  )
  check(
    'A5-scroll',
    '横向滚动实测（业主已裁决「接受滚动」，此处只记录数字不判失败）',
    true,
    '仅记录',
    scrollBox && {
      scrollWidth: scrollBox.scrollWidth,
      clientWidth: scrollBox.clientWidth,
      overflowPx: scrollBox.scrollWidth - scrollBox.clientWidth,
      hasHScroll: scrollBox.hasHScroll,
      overflowX: scrollBox.overflowX,
    },
  )

  // ---------- A8：平局稳定性（page_size=10 翻 3 页，倒序头部连续） ----------
  // 顺序刻意排在 A4 之前：这里是「默认 desc 首屏」的原始状态。
  // （反过来先切 asc 再点回 desc 会命中 React Query 缓存——Providers.tsx 全局 staleTime=30s，
  //   同一 queryKey 在窗口期内不重发请求——于是拿不到「第 2 条 desc 请求」，等于用缓存行为干扰断言。）
  // 翻到第 2、3 页：点分页「下一页」，只点可见的那个（桌面/移动两套 PaginationBar 同时在 DOM 里）。
  const pagesResp = [first]
  for (const target of ['2', '3']) {
    const before = desktopAssets.length
    await page.locator('button[aria-label="下一页"]:visible').first().click()
    const resp = await waitAssetMatching(page, desktopAssets, before, (e) => e.page === target)
    pagesResp.push(resp)
  }
  const pageRows = pagesResp.map((r) => ({
    page: r.page,
    pageSize: r.pageSize,
    count: r.items.length,
    ids: r.items.map((i) => i.id),
    updatedAts: r.items.map((i) => i.updated_at),
  }))
  metrics.a8 = { pages: pageRows }

  const allIds = pageRows.flatMap((p) => p.ids)
  const uniqueIds = new Set(allIds)
  check(
    'A8-no-dup',
    'page_size=10 三页 id 集合无交集（同一行不跨页重复）',
    allIds.length === uniqueIds.size && pageRows.every((p) => p.count === 10),
    '每页 10 条、30 个 id 全不重复',
    { rows: allIds.length, unique: uniqueIds.size, counts: pageRows.map((p) => p.count), pageSizes: pageRows.map((p) => p.pageSize) },
  )

  const nonIncreasing = (arr) => arr.every((v, i) => i === 0 || arr[i - 1] >= v)
  const perPageMono = pageRows.every((p) => nonIncreasing(p.updatedAts))
  // 跨页连续性：下一页首条不得比上一页末条更新（倒序口径下页与页之间也要单调）
  const crossPageMono = pageRows.every((p, i) => i === 0 || pageRows[i - 1].updatedAts.slice(-1)[0] >= p.updatedAts[0])
  check(
    'A8-order',
    '三页内 updated_at 逐页非递增，且跨页首尾接续（倒序头部连续 30 条）',
    perPageMono && crossPageMono,
    '每页非递增 + 页间连续',
    { perPageMono, crossPageMono, tails: pageRows.map((p) => [p.updatedAts[0], p.updatedAts.slice(-1)[0]]) },
  )

  // 兜底键 D2：同刻组内 id 必须严格降序（缺 tiebreaker 时 SQLite 通常回 rowid 升序 → 此条为红）
  const tieBreakOk = pageRows.every((p) =>
    p.ids.every((id, i) => i === 0 || p.updatedAts[i] !== p.updatedAts[i - 1] || id < p.ids[i - 1]),
  )
  check(
    'A8-tiebreak',
    '同刻组内 id 严格降序（spec D2 的兜底键在浏览器侧同样成立）',
    tieBreakOk,
    '相同 updated_at 的相邻行 id 递减',
    { tieBreakOk, sample: { ids: pageRows[0].ids.slice(0, 8), updatedAts: pageRows[0].updatedAts.slice(0, 8) } },
  )

  // ---------- A4：点表头一次 → asc + aria-sort 翻转 ----------
  // 此时停在第 3 页 desc；点表头 = setSort(asc) + setPage(1)，`asc` 这个 queryKey 尚未取过 → 必然发请求。
  const beforeSort = desktopAssets.length
  await page
    .locator(`table[aria-label="${TABLE_LABEL}"] thead button`)
    .filter({ hasText: '更新时间' })
    .first()
    .click()
  const ascResp = await waitAssetMatching(page, desktopAssets, beforeSort, (e) => e.sortOrder === 'asc')
  await page.waitForFunction(
    () => {
      const idx = window.__pfProbe.updatedCol()
      const ths = window.__pfProbe.headers()
      return idx >= 0 && ths[idx].ariaSort === 'ascending'
    },
    null,
    { timeout: 30000 },
  )
  const headersAsc = await page.evaluate(() => window.__pfProbe.headers())
  const updAsc = updIdx >= 0 ? headersAsc[updIdx] : null
  await page.waitForTimeout(200)
  await shot(page, 'ledger-asc-1536.png')
  metrics.sortAsc = { sortField: ascResp.sortField, sortOrder: ascResp.sortOrder, ariaSort: updAsc && updAsc.ariaSort }
  check(
    'A4-request',
    '点击「更新时间」表头一次 → 下一次 GET /api/assets 的 sort_order=asc',
    ascResp.sortOrder === 'asc' && ascResp.sortField === 'updated_at',
    { sort_field: 'updated_at', sort_order: 'asc' },
    { sort_field: ascResp.sortField, sort_order: ascResp.sortOrder, page: ascResp.page, url: ascResp.url },
  )
  check(
    'A4-aria',
    '点击后 aria-sort 翻转为 ascending',
    !!updAsc && updAsc.ariaSort === 'ascending',
    'ascending',
    updAsc && updAsc.ariaSort,
  )

  metrics.desktopRequests = desktopAssets.map((e) => ({
    page: e.page,
    page_size: e.pageSize,
    sort_field: e.sortField,
    sort_order: e.sortOrder,
    count: e.items ? e.items.length : null,
  }))
  notes.push(`桌面 ledger 运行期 console.error / pageerror ${errors.length} 条${errors.length ? `：${stable(errors.slice(0, 3))}` : ''}`)

  // =====================================================================
  // A7：/records?view=current 首屏不受影响
  // =====================================================================
  const ctxCurrent = await browser.newContext({ viewport: DESKTOP })
  const pageCurrent = await ctxCurrent.newPage()
  await pageCurrent.addInitScript({ content: PAGE_HELPERS })
  const currentAssets = attachRecorder(pageCurrent)
  /** 当前值读模型（`/api/assets/current`）的请求参数，用于判定「当前值视图自身的取数是否被改动波及」。 */
  const currentModel = []
  pageCurrent.on('response', (res) => {
    try {
      const u = new URL(res.url())
      if (u.pathname !== '/api/assets/current') return
      currentModel.push({
        path: u.pathname,
        query: Object.fromEntries(u.searchParams.entries()),
        status: res.status(),
      })
    } catch {
      /* ignore */
    }
  })

  await pageCurrent.goto(BASE + '/records?view=current', { waitUntil: 'domcontentloaded', timeout: 120000 })
  // 等当前值读模型落地，再留一段观察窗，确保迟到的 /api/assets 也能被记账。
  await waitFor(pageCurrent, () => currentModel.length > 0, 60000, '当前值视图 /api/assets/current 响应').catch(
    () => null,
  )
  await pageCurrent.waitForTimeout(1500)
  await shot(pageCurrent, 'current-desktop-1536.png')

  const currentWithSort = currentAssets.filter((e) => e.sortField === 'updated_at')
  metrics.a7 = {
    ledgerRequests: currentAssets.map((e) => ({
      url: e.url,
      page: e.page,
      sort_field: e.sortField,
      sort_order: e.sortOrder,
    })),
    currentModelRequests: currentModel,
  }
  // A7 分两半：一半是「当前值视图自己的取数」有没有被波及（应通过），
  // 一半是 spec §3 第 7 条的字面口径「?view=current 请求参数不变」（本条当前为红，见 note）。
  check(
    'A7-model',
    '当前值视图自身的读模型请求（/api/assets/current）参数不含任何排序字段',
    currentModel.length > 0 && currentModel.every((r) => !('sort_field' in r.query) && !('sort_order' in r.query)),
    '至少 1 条 /api/assets/current，且 query 无 sort_field / sort_order',
    currentModel.length ? currentModel.map((r) => r.query) : '未捕获到 /api/assets/current',
  )
  check(
    'A7',
    'spec §3-7 字面口径：/records?view=current 首屏的任何 /api/assets 请求都不带 sort_field=updated_at',
    currentWithSort.length === 0,
    '0 条带 sort_field=updated_at 的 /api/assets 请求',
    { matched: currentWithSort.length, requests: metrics.a7.ledgerRequests },
  )
  if (currentWithSort.length > 0) {
    notes.push(
      'A7 红的原因（非探针误判）：页面级 `listQuery`（records/page.tsx:1767-1773）无 `enabled` 门、不受 `view` 约束，' +
        '`?view=current` 也会发一条分页+排序的 `/api/assets` 请求；本次默认排序改成 updated_at 后，这条请求的参数随之变化。' +
        '`git show HEAD:frontend/src/app/records/page.tsx` 显示改前同一条查询无条件发起（默认 sort=asset_date desc），' +
        '即「请求数量与顺序不变，参数值随默认排序变」。当前值视图自身数据来自 /api/assets/current（A7-model 已绿）。' +
        '是否算违反 §3-7、是否给 listQuery 加 enabled=view==="ledger"，属口径/改动面裁决，探针不改口径。',
    )
  }

  // =====================================================================
  // A6：移动 390×844 卡片
  // =====================================================================
  const ctxMobile = await browser.newContext({ viewport: MOBILE })
  const pageMobile = await ctxMobile.newPage()
  await pageMobile.addInitScript({ content: PAGE_HELPERS })
  const mobileAssets = attachRecorder(pageMobile)
  await pageMobile.goto(BASE + '/records?view=ledger', { waitUntil: 'domcontentloaded', timeout: 120000 })
  await waitAssetMatching(pageMobile, mobileAssets, 0)
  await pageMobile.waitForFunction(() => window.__pfProbe.card(0) !== null, null, { timeout: 60000 })
  await pageMobile.waitForTimeout(300)
  // 卡片高度 + 页头 > 844，直接把首张卡片滚到视口中间再拍，否则「更新时间」meta 行会落在截图外。
  await pageMobile.evaluate(() => {
    const li = document.querySelectorAll('ul[aria-label="资产记录列表"] > li')[0]
    if (li) li.scrollIntoView({ block: 'center' })
  })
  await pageMobile.waitForTimeout(200)
  await shot(pageMobile, 'ledger-mobile-390.png')

  const card0 = await pageMobile.evaluate(() => window.__pfProbe.card(0))
  const cardCount = await pageMobile.evaluate(() => window.__pfProbe.cardCount())
  const docW = await pageMobile.evaluate(() => window.__pfProbe.docWidth())
  metrics.a6 = { cardCount, card: card0, docWidth: docW }
  check(
    'A6-meta',
    '390px 卡片内存在 label 为「更新时间」的 meta 项，值匹配 YYYY/MM/DD HH:mm',
    !!card0 && !!card0.value && CELL_TEXT_RE.test(card0.value),
    String(CELL_TEXT_RE),
    card0 && { labels: card0.labels, value: card0.value },
  )
  check(
    'A6-width',
    '「更新时间」meta 项宽度不溢出自卡（scrollWidth ≤ clientWidth + 1）',
    !!card0 && card0.scrollWidth <= card0.clientWidth + 1,
    'scrollWidth ≤ clientWidth + 1',
    card0 && { scrollWidth: card0.scrollWidth, clientWidth: card0.clientWidth },
  )
  // span: 2 是 spec D4 落地时量测出的排版收口（业主裁决过），不属 §3 断言项：
  // 只记录是否生效，不让它影响退出码（并发会话若临时撤掉 span，也不该把 A1–A8 判红）。
  notes.push(
    `390px「更新时间」meta 独占整行（mobile.span=2）：${/col-span-2/.test(String(card0 && card0.wrapperClass)) ? '生效' : '未生效'}` +
      `（class=${card0 && card0.wrapperClass}）`,
  )
  notes.push(
    `390px 页面级横向滚动：scrollWidth=${docW.scrollWidth} / clientWidth=${docW.clientWidth}` +
      (docW.scrollWidth > docW.clientWidth + 1 ? '（有溢出）' : '（无溢出）'),
  )

  await browser.close()

  const failed = checks.filter((c) => !c.pass)
  result.metrics = metrics
  result.passed = checks.length - failed.length
  result.failed = failed.length
  console.log('PROBE_RESULT ' + JSON.stringify(result))
  process.exit(failed.length === 0 ? 0 : 1)
})().catch((e) => {
  console.error('FATAL', e)
  process.exit(2)
})