/**
 * 数据域表格「零截断」四层判据浏览器探针（playwright-core，无 test runner 依赖）。
 *
 * 对应 spec `.scratch/table-width/spec.md` D12/D13 + A4（四层判据）、A12（移动卡片）、
 * A14（门禁接入）；方法口径来自 `.scratch/table-width/03-report.md` §C.1–C.6：
 *
 *   L1 自身滚动溢出   th/td 的 `scrollWidth − clientWidth > 1`
 *   L2 后代裁剪       后代元素 `overflow-x ∈ {hidden, clip}` 且 `scrollWidth − clientWidth > 1`
 *                     （本项目把 truncate 写在内层 span/button 上，只看 td 会漏检 34px —— §C.1 D/E 用例）
 *   L3 文本墨盒越盒   单元格内文本 Range 墨盒的并集越出该单元格内容盒
 *                     （inline 元素 clientWidth 恒 0，所以文本宽度只能用 Range 量 —— §C.1 G 用例；
 *                      03-report 限定「只在 nowrap 下启用」，实测 `inline-flex` 的「金额 + 负债」
 *                      是 normal，只测 nowrap 会漏掉它 → 改为整格文本墨盒并集，换行文本自然不误报）
 *   L4 不透明盒越盒   img/label/pill/button 等有底色或替换元素，border box 越出单元格内容盒
 *                     （命中「44px 命中区塞进 24px 内容盒」「100px 缩略图塞进 88px」这类不是文本的溢出）
 *
 * 阈值 1px、不加内边距补偿（clientWidth 本身已是 padding box，§C.1 结论）、`.pf-num` 不做特例（§C.4）。
 * 只测 `th/td` 及其后代，不测外层容器；表格整体在容器内横滚属合法（P3/04 裁决 1）。
 *
 * **A7–A10 冻结列四件套**（spec D9/D10 + §6.3 R3.7：验收面 = 全部出表列组）与四层判据同视口、
 * 同一次导航里测（`scanFrozen`）：横滚贴边 ≤1px、冻结格背景 alpha=1 且 `elementFromPoint`
 * 不透字、首末列 inset 阴影方向与色值（`--pf-line`）、z 序 10/20/20。**未声明冻结列的表
 * 记 `declared:false` 并判 A7–A10 不成立**（不静默跳过 —— 那会把「没实现」伪装成「通过」）。
 *
 * 视口集（spec D12）：桌面 9 —— 767/768、1023/1024、1279/1280、1440、1600、1920；
 * 移动 2 —— 390/320（只验卡片字段集合与卡片内溢出，不验表格）。767 属卡片侧（`md:block` 在 ≥768）。
 *
 * 「合法省略」白名单（04 裁决 2 = 甲+乙同时成立）：甲 列配置声明 `ellipsis: true`
 * （由 `frontend/scripts/table_width_audit.py --emit-ellipsis-allowlist` 从源码抽出，
 * 经环境变量 ELLIPSIS_ALLOWLIST 传入 `{表名: [列名…]}`）；乙 DOM 该单元格内确实给出
 * `title` 全文。只有甲无乙、或只有乙无甲都不算合法，照常判 FAIL。
 * **白名单不得手工塞已知缺陷**：它只由源码里的 `ellipsis: true` 生成。
 *
 * 用法（通常由 scripts/verify_table_width.sh 调用；也可单独跑，需已起前后端）：
 *   BASE=http://127.0.0.1:6681 OUT_JSON=.scratch/table-width/evidence/matrix.json \
 *   SHOT_DIR=.scratch/table-width/evidence/shots bun scripts/table_width_probe.js
 *   TW_GROUPS=records-ledger,evidence TW_VIEWPORTS=1440,1920 bun …   # 只跑子集（调试用）
 *
 * 输出：最后一行 `PROBE_RESULT <json>`（紧凑摘要，含 matrix 文件路径）；完整矩阵写入 OUT_JSON。
 * 退出码：0 全 PASS；1 有 FAIL；2 环境/运行失败。
 */
const path = require('path')
const fs = require('fs')
const PW = require(path.join(__dirname, '..', 'frontend', 'node_modules', 'playwright-core'))

const BASE = process.env.BASE || 'http://127.0.0.1:6681'
const OUT_JSON = process.env.OUT_JSON || ''
const SHOT_DIR = process.env.SHOT_DIR || ''
/**
 * 仓库根 + 相对化工具：证据 JSON 里**不能出现绝对路径**（`scripts/check-public-safety.sh`
 * 的 PATTERNS 里有「本机/服务器绝对路径」一条，见该脚本第 29 行）。凡是要写进
 * `matrix.json` 的路径（截图清单、matrixPath）都过一遍 relToRepo；
 * 即使证据目录被指到仓库外，也只写 `../../…` 形式的相对路径，绝不落绝对路径。
 */
const REPO_ROOT = path.resolve(__dirname, '..')
const relToRepo = (p) => (p ? path.relative(REPO_ROOT, path.resolve(p)) || path.basename(p) : p)
const MAX_FAIL_SHOTS = Number(process.env.MAX_FAIL_SHOTS || 40)
const ELLIPSIS_ALLOWLIST_FILE = process.env.ELLIPSIS_ALLOWLIST || ''

/** 桌面 9 个视口（spec D12；767 是 md 边界的卡片侧，仅测卡片）。 */
const DESKTOP_WIDTHS = (process.env.TW_VIEWPORTS
  ? process.env.TW_VIEWPORTS.split(',').map((w) => Number(w.trim()))
  : [767, 768, 1023, 1024, 1279, 1280, 1440, 1600, 1920]).filter((w) => Number.isFinite(w) && w > 0)
/** 移动 2 个视口（只验卡片字段对齐 + 卡片内不溢出）。 */
const MOBILE_WIDTHS = [390, 320]
const DESKTOP_HEIGHT = 900
const MOBILE_HEIGHT = 844
/** 表格渲染断点：DataTable 桌面表格 `hidden … md:block`（< 768 走卡片）。 */
const TABLE_MIN_WIDTH = 768
/** 判据阈值（px）：§C.1 量化误差 ≤ 0.5px，取 1px 不会因缩放抖动误报。 */
const TOL = 1
/**
 * 判据 id（矩阵列维度）。L5 是**在四层之外补的一层**，理由见 03-report §C.2 的边界：
 * 「记录·操作列」的已知缺陷不是内容溢出，而是图标按钮被 flex 压到 38px（44px 命中区破防）——
 * 此时 L1–L4 全绿（实测：`flexShrink:1` 把按钮压小、文本没溢出、没有 overflow:hidden），
 * 唯一可测的信号是「可点区域小于 `--pf-control-hit`」。不加这层，操作列必然漏检。
 * 作用域收窄到「操作」列，避免把正文里合法的窄按钮（px-1 的文字按钮）算进来。
 */
const JUDGES = ['L1-self', 'L2-desc-clip', 'L3-text-ink', 'L4-opaque-box', 'L5-hit-target']

/**
 * 12 个列组（spec D5）：DataTable 8 组 + 原生表 4 组。
 * `table` 是页内定位器：DataTable 组用 `aria-label`（组件把 label 同时给 table 与 ul），
 * 原生表用表头文本定位（类名会随样式改动漂移，表头文本是这一票唯一稳定的锚）。
 */
const GROUPS = [
  {
    id: 'records-ledger', name: '记录明细', route: '/records?view=ledger',
    table: { label: '资产记录列表' },
    // 金额列按金额倒序 → 库内最大金额与合成「负债」大额行同时落到第一页（否则金额列的
    // 溢出量取决于当前分页里的数据，不可复算）。这是数据铺排，不是判据放宽。
    prep: [{ sortDesc: '金额（元）' }],
  },
  { id: 'records-current', name: '当前值', route: '/records?view=current', table: { label: '资产当前值' } },
  {
    id: 'expenses-flow', name: '支出流水', route: '/expenses',
    table: { label: '支出流水' }, prep: [{ tab: '支出口流水' }],
  },
  {
    id: 'expenses-template', name: '支出模板', route: '/expenses',
    table: { label: '固定支出项目' }, prep: [{ tab: '固定支出项目' }],
  },
  { id: 'evidence', name: '凭证列表', route: '/data/evidence', table: { label: '截图凭证' } },
  {
    id: 'export-preview', name: '导出预览', route: '/export',
    // 预览表由 `filters.period !== 'all'` 门控（默认 all → query 不 enabled → 表不渲染），
    // 所以先把「账期类型」选成按年，这是取数准备，不是判据放宽。
    table: { label: '导出数据预览' },
    prep: [{ tab: '数据导出' }, { selectNative: { selector: 'select:has(option[value="year"])', value: 'year' } }],
  },
  {
    id: 'export-rules', name: '导出规则', route: '/export',
    table: { label: '自动导出规则' }, prep: [{ tab: '自动导出规则' }],
  },
  { id: 'export-history', name: '导出历史', route: '/export', table: { label: '导出历史' } },
  {
    id: 'analysis-detail', name: '分析·逐项明细', route: '/analysis',
    // 逐项明细卡在「升值」tab 里（AppreciationTab），默认 tab 是「净资产趋势」。
    table: { thFirst: '资产项', thIncludes: ['市值', '本金口径'] },
    prep: [{ clickText: '升值' }],
  },
  {
    id: 'allocation-analysis', name: '归集分析', route: '/allocation/asset_class',
    table: { thFirst: '项目' },
    // spec D8：AnalysisTable 保持「xl 才出表」，< 1280 走其自研卡片 → 不跑表格断言。
    minTableWidth: 1280, note: 'D8 例外：<1280 走 AnalysisTable 自研卡片',
  },
  {
    id: 'records-bulk-edit', name: '批量修改弹窗', route: '/records?view=ledger',
    table: { thFirst: '资产名称', thIncludes: ['日期'] },
    // 弹窗表要「勾一行 → 点批量修改」两步交互才出现在 DOM 里；探针按 prep 显式打开，
    // 打不开就记 skipped（不给假绿，也不阻塞其它列组）。
    prep: [{ clickText: '批量修改' }, { pickMultiSelect: 3 }],
    optional: true,
  },
  {
    id: 'import-preview', name: '导入预览', route: '/import',
    table: { thFirst: '行号' },
    // 预览表只在「解析完一个文件」后才渲染，探针不造上传动作 → 恒 skipped，
    // 但列组留在矩阵里（机器可比对的形状不缺口）。
    optional: true, skipReason: '需真实上传解析才渲染预览表（不在探针内造上传）',
  },
]

/**
 * spec §1.3 记录的 9 个已知缺陷（硬门：在**未改造**代码上必须全部命中）。
 *
 * `specOver` = spec 记录值（= 列宽 − 内容盒 与「最坏内容」的差额，由
 * `frontend/scripts/table_width_audit.py --expect-known-defects` 逐条复算）；
 * `renderOver` = 本探针在渲染层能量到的量级（两者不等时说明缺陷形态变了，不是判据放宽）：
 *   · 操作列：spec 的 21.0px 是**列宽**赤字（236 vs 需要 241.02），渲染层的实际症状是
 *     3 个图标按钮被 flex 压到 38px → L5 量到 6px 命中区赤字，两者是同一件事的两面。
 *   · 状态胶囊 4.1px：spec 值来自档位口径（`label` 档最坏内容 114.05 vs 内容盒 110），
 *     而当前值表的状态值域只有「最新/待更新/未录入」（≤3 字），**只读渲染不出 114px**；
 *     写操作后的「已保存 · 撤销」态才可能撑宽 —— 只读探针结构上测不到，由静态审计层兜。
 *   · 使用情况：spec 的 114.05 按「已使用 · 1,024 行」标定，渲染值无千分位（「1024」），
 *     故渲染量级比 spec 小几 px。
 */
const KNOWN_DEFECTS = [
  { id: 'records-updated-at', group: 'records-ledger', col: '更新时间', specOver: 7.4, renderOver: 7.4, tol: 2.5, judges: ['L3-text-ink'] },
  { id: 'records-amount', group: 'records-ledger', col: '金额（元）', specOver: 25.4, renderOver: 25.4, tol: 5, judges: ['L3-text-ink', 'L4-opaque-box'], variant: 'plain' },
  { id: 'records-amount-liability', group: 'records-ledger', col: '金额（元）', specOver: 70.9, renderOver: 70.9, tol: 8, judges: ['L3-text-ink', 'L4-opaque-box'], variant: 'liability' },
  { id: 'records-classification', group: 'records-ledger', col: '分类', specOver: 37.4, renderOver: 45.7, tol: 10, judges: ['L4-opaque-box', 'L3-text-ink'], mask: '合成行取「补分类 · 缺 4」（比 spec 的 97.39 基准更宽），故渲染量级比 spec 略大' },
  { id: 'records-actions', group: 'records-ledger', col: '操作', specOver: 21.0, renderOver: 6, tol: 3, judges: ['L5-hit-target'] },
  { id: 'records-current-status', group: 'records-current', col: '状态', specOver: 4.1, renderOver: null, tol: 3, judges: ['L3-text-ink', 'L4-opaque-box'], mask: '状态值域只有「最新/待更新/未录入」（≤3 字），只读渲染撑不到 114.05；该 4.1px 是档位口径值，只能静态审计层判' },
  { id: 'evidence-thumb', group: 'evidence', col: '缩略图', specOver: 12, renderOver: null, tol: 3, judges: ['L4-opaque-box'], mask: '凭证表 minWidth(1000) > 声明列和(740)，列被等比放大 → 缩略图渲染宽 140（内容盒 100）恰好放下；12px 是「按声明宽度 128 算」的档位不足，静态审计层判' },
  { id: 'evidence-usage', group: 'evidence', col: '使用情况', specOver: 24.1, renderOver: null, tol: 8, judges: ['L3-text-ink', 'L4-opaque-box'], mask: '同上：等比放大后内容盒 110.73 ≈ 胶囊 110.73，渲染层不溢出；且渲染值无千分位（「1024」而非「1,024」）' },
  { id: 'records-select-hit', group: 'records-ledger', col: '（选择列）', specOver: 20, renderOver: 20, tol: 3, judges: ['L4-opaque-box', 'L1-self'] },
]

const ellipsisAllowlist = (() => {
  if (!ELLIPSIS_ALLOWLIST_FILE) return {}
  try {
    return JSON.parse(fs.readFileSync(ELLIPSIS_ALLOWLIST_FILE, 'utf8'))
  } catch (e) {
    console.error(`警告：读不了省略白名单 ${ELLIPSIS_ALLOWLIST_FILE}（${e.message}），按空白名单继续`)
    return {}
  }
})()

/**
 * 页内取数工具（`page.addInitScript` 注入，每次导航自动可用）。
 *
 * 全部测量都在视口坐标系里做：单元格内容盒 = border box 去掉 border 与 padding，
 * 墨盒 = 文本 Range 的包围盒 / 元素 border box。判定只在「墨盒 vs 内容盒」之间比，
 * 不与列宽或表宽比 —— fixed 布局下容器更宽会等比放大列宽（§C.1 结论：只能比内容盒）。
 */
const PAGE_HELPERS = `
window.__pfWidth = (() => {
  const TOL = ${TOL}
  const r2 = (v) => Math.round(v * 100) / 100
  const text = (el) => (el && el.textContent ? el.textContent : '').replace(/\\s+/g, ' ').trim()
  const num = (v) => { const n = parseFloat(v); return Number.isFinite(n) ? n : 0 }

  const isSrOnly = (el) => {
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      if (n.classList && n.classList.contains('sr-only')) return true
    }
    return false
  }
  const visible = (el) => {
    const s = getComputedStyle(el)
    if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false
    const r = el.getBoundingClientRect()
    return r.width > 0 && r.height > 0
  }
  /** 内容盒（padding box 去掉左右内边距，坐在视口坐标系里）。 */
  const contentBox = (el) => {
    const s = getComputedStyle(el)
    const r = el.getBoundingClientRect()
    const left = r.left + num(s.borderLeftWidth) + num(s.paddingLeft)
    const right = r.right - num(s.borderRightWidth) - num(s.paddingRight)
    return { left, right, width: right - left, borderRight: r.right, borderLeft: r.left }
  }
  /** 文本墨盒：遍历文本节点取 Range 包围盒的并集（inline 元素 clientWidth=0，只能这么量）。 */
  const textInk = (root) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
    let min = Infinity
    let max = -Infinity
    let node
    while ((node = walker.nextNode())) {
      const value = node.nodeValue || ''
      if (!value.trim()) continue
      const pe = node.parentElement
      if (!pe || isSrOnly(pe)) continue
      const s = getComputedStyle(pe)
      if (s.display === 'none' || s.visibility === 'hidden') continue
      const range = document.createRange()
      range.selectNodeContents(node)
      const box = range.getBoundingClientRect()
      if (box.width === 0 && box.height === 0) continue
      if (box.left < min) min = box.left
      if (box.right > max) max = box.right
    }
    if (!Number.isFinite(min)) return null
    return { left: min, right: max }
  }
  const alphaOf = (color) => {
    const m = String(color || '').match(/rgba?\\(([^)]+)\\)/)
    if (!m) return 0
    const parts = m[1].split(',').map((x) => parseFloat(x))
    return parts.length > 3 ? parts[3] : 1
  }

  const findTable = (spec) => {
    if (!spec) return null
    if (spec.label) {
      const t = document.querySelector('table[aria-label="' + spec.label + '"]')
      if (t && t.getClientRects().length) return t
    }
    const tables = [...document.querySelectorAll('table')].filter((t) => t.getClientRects().length)
    for (const t of tables) {
      const ths = [...t.querySelectorAll('thead th')].map(text)
      if (spec.thFirst && ths[0] === spec.thFirst) {
        if (!spec.thIncludes || spec.thIncludes.every((h) => ths.includes(h))) return t
      }
    }
    return null
  }

  const cellName = (th, index) => {
    if (!th) return '#' + index
    const t = text(th)
    if (t) return t
    if (th.querySelector('input[type=checkbox]')) return '（选择列）'
    return '（空列）'
  }

  /** 合法省略判定（04 裁决 2：甲 ellipsis:true + 乙 DOM title 全文）。 */
  const legalEllipsis = (tableLabel, columnName, cell) => {
    const allow = (window.__pfWidthAllowlist || {})[tableLabel] || []
    if (!allow.includes(columnName)) return false
    const withTitle = [cell, ...cell.querySelectorAll('*')].some(
      (el) => (el.getAttribute && (el.getAttribute('title') || '').trim().length > 0),
    )
    return withTitle
  }

  const scanCell = (cell, columnName, kind, rowIndex, tableLabel) => {
    const out = []
    const cb = contentBox(cell)
    const cs = getComputedStyle(cell)
    const preview = text(cell).slice(0, 40)
    const base = { row: rowIndex, col: columnName, kind, text: preview }

    // ---- L1 自身滚动溢出 ---------------------------------------------------
    const selfOver = cell.scrollWidth - cell.clientWidth
    if (selfOver > TOL) {
      out.push({ ...base, judge: 'L1-self', over: r2(selfOver), detail: { sw: cell.scrollWidth, cw: cell.clientWidth } })
    }

    // ---- L2 后代裁剪（truncate 写在内层 span/button 上） --------------------
    for (const el of cell.querySelectorAll('*')) {
      if (isSrOnly(el)) continue
      const s = getComputedStyle(el)
      if (s.display === 'none' || s.visibility === 'hidden') continue
      if (!['hidden', 'clip'].includes(s.overflowX)) continue
      if (el.clientWidth === 0) continue
      const over = el.scrollWidth - el.clientWidth
      if (over <= TOL) continue
      out.push({
        ...base, judge: 'L2-desc-clip', over: r2(over),
        detail: { tag: el.tagName.toLowerCase(), cls: String(el.className || '').slice(0, 70), sw: el.scrollWidth, cw: el.clientWidth },
      })
    }

    // ---- L3 文本墨盒越出内容盒 ---------------------------------------------
    // 口径：单元格内**所有文本**的 Range 墨盒并集 vs 内容盒。换行的文本并集仍在盒内，
    // 所以不需要「只在 nowrap 下启用」这条限制（03-report §C.2 的顾虑是浮点抖动，
    // 这里同样用 1px 阈值兜住）；反过来，「inline-flex」里并排的「金额 + 负债」这种
    // 非 nowrap 容器内的溢出也能被抓到——只测 nowrap 元素会漏掉它（实测金额含负债行）。
    const ink = textInk(cell)
    if (ink) {
      const rightOver = ink.right - cb.right
      const leftOver = cb.left - ink.left
      const over = Math.max(rightOver, leftOver)
      if (over > TOL) {
        out.push({
          ...base, judge: 'L3-text-ink', over: r2(over),
          detail: {
            side: rightOver >= leftOver ? 'right' : 'left',
            textW: r2(ink.right - ink.left),
            box: r2(cb.width),
            whiteSpace: cs.whiteSpace,
          },
        })
      }
    }

    // ---- L4 不透明盒越出内容盒 -------------------------------------------
    for (const el of cell.querySelectorAll('*')) {
      if (isSrOnly(el)) continue
      const s = getComputedStyle(el)
      if (s.display === 'none' || s.visibility === 'hidden') continue
      const r = el.getBoundingClientRect()
      if (r.width <= 0 || r.height <= 0) continue
      const alpha = alphaOf(s.backgroundColor)
      const hasBorder = num(s.borderLeftWidth) + num(s.borderRightWidth) > 0
      const replaced = ['IMG', 'SVG', 'INPUT', 'BUTTON', 'LABEL', 'SELECT', 'TEXTAREA', 'CANVAS'].includes(el.tagName)
      const painted = alpha > 0.01 || hasBorder || replaced
      if (!painted) continue
      const rightOver = r.right - cb.right
      const leftOver = cb.left - r.left
      const over = Math.max(rightOver, leftOver)
      if (over <= TOL) continue
      out.push({
        ...base, judge: 'L4-opaque-box', over: r2(over),
        detail: {
          side: rightOver >= leftOver ? 'right' : 'left',
          tag: el.tagName.toLowerCase(),
          cls: String(el.className || '').slice(0, 70),
          boxW: r2(r.width),
          contentW: r2(cb.width),
          crossesCell: r.right > cb.borderRight + TOL || r.left < cb.borderLeft - TOL,
          opaque: alpha > 0.01,
        },
      })
    }
    // ---- L5 命中区压缩（只测「操作」列；图标按钮被 flex 压到 44px 以下）------
    if (columnName === '操作') {
      const hit = (() => {
        const v = getComputedStyle(document.documentElement).getPropertyValue('--pf-control-hit')
        const n = parseFloat(v)
        return Number.isFinite(n) && n > 0 ? n : 44
      })()
      const interactive = [...cell.querySelectorAll('button, a, label, input, select, textarea')].filter(
        (el) => !isSrOnly(el) && visible(el) && !el.parentElement.closest('button, a, label'),
      )
      for (const el of interactive) {
        const r = el.getBoundingClientRect()
        const short = hit - Math.min(r.width, r.height)
        if (short <= TOL) continue
        out.push({
          ...base, judge: 'L5-hit-target', over: r2(short),
          detail: {
            tag: el.tagName.toLowerCase(),
            boxW: r2(r.width), boxH: r2(r.height), hit: r2(hit),
            label: (el.getAttribute('aria-label') || text(el) || '').slice(0, 30),
          },
        })
      }
    }

    return out
  }

  const scan = (spec) => {
    const t = findTable(spec)
    if (!t) return { found: false }
    const tableLabel = t.getAttribute('aria-label') || ''
    const ths = [...t.querySelectorAll('thead th')]
    const headers = ths.map((th, i) => cellName(th, i))
    const violations = []
    const cells = []

    // 表头 th（表头文字同样不得被压到邻列）
    for (const [i, th] of ths.entries()) {
      const name = headers[i]
      const recs = scanCell(th, name, 'th', -1, tableLabel)
      cells.push({ row: -1, col: name, kind: 'th', over: recs.length ? Math.max(...recs.map((v) => v.over)) : 0 })
      if (recs.length && !legalEllipsis(tableLabel, name, th)) violations.push(...recs)
    }

    // 表体 td
    const rows = [...t.querySelectorAll('tbody tr')]
    for (const [ri, tr] of rows.entries()) {
      for (const [ci, td] of [...tr.children].entries()) {
        if (td.tagName !== 'TD') continue
        const name = ci < headers.length ? headers[ci] : '#' + ci
        const recs = scanCell(td, name, 'td', ri, tableLabel)
        cells.push({ row: ri, col: name, kind: 'td', over: recs.length ? Math.max(...recs.map((v) => v.over)) : 0 })
        if (recs.length && !legalEllipsis(tableLabel, name, td)) violations.push(...recs)
      }
    }

    const scroller = t.parentElement
    const container = scroller ? scroller.parentElement : null
    const box = scroller && getComputedStyle(scroller)
    return {
      found: true,
      tableLabel,
      headers,
      headerCount: headers.length,
      rowCount: rows.length,
      cellCount: cells.length,
      violations,
      // 每列每判据的 max 溢出量（矩阵的列维度）
      colSummary: headers.map((h) => {
        const vs = violations.filter((v) => v.col === h)
        const perJudge = {}
        for (const j of ${JSON.stringify(JUDGES)}) {
          const jv = vs.filter((v) => v.judge === j)
          if (jv.length) perJudge[j] = r2(Math.max(...jv.map((v) => v.over)))
        }
        return { col: h, maxOver: vs.length ? r2(Math.max(...vs.map((v) => v.over))) : 0, judges: perJudge, count: vs.length }
      }).filter((c) => c.count > 0),
      scroller: scroller ? {
        clientWidth: scroller.clientWidth,
        scrollWidth: scroller.scrollWidth,
        overflowX: scroller.scrollWidth - scroller.clientWidth,
        cssOverflowX: box ? box.overflowX : null,
      } : null,
      tableWidth: r2(t.getBoundingClientRect().width),
      containerWidth: container ? container.clientWidth : null,
      page: { scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth },
    }
  }

  /**
   * A7–A10 冻结列四件套（spec D9/D10 + §3 A7–A10）。口径与票 01 的 46/46 冒烟一致：
   *
   *   A7 贴边：横滚到最右 → 首列左缘贴滚动容器左缘 ≤1px、操作列右缘贴右缘 ≤1px；
   *            横滚到最左 → 首列仍贴边；
   *   A8 不透明：冻结格（表体首列/操作列 + 表头首列/操作列）computed background-color
   *            alpha = 1，且 elementFromPoint 打在冻结格边界上取到的元素属于该冻结格
   *            （邻列文字不得透出）；
   *   A9 边界可见：首列 box-shadow 含 inset -1px 0 0、操作列含 inset 1px 0 0，
   *            阴影色 = 解析后的 --pf-line；
   *   A10 z 序：表体冻结格 10、thead 20、表头首列交叉格 20。
   *
   * **不静默跳过未实现冻结列的表**：没有 position:sticky 的首列时如实给 declared:false
   * 与 A7–A10 违规 —— 否则「没实现」会被伪装成「通过」。
   * 注意：本段在注入的模板字符串里，注释与代码都**不能出现反引号**（会提前结束模板）。
   */
  const rgbOf = (value) => {
    const m = String(value || '').match(/rgba?\\(([^)]+)\\)/)
    if (!m) return null
    const parts = m[1].split(/[,/\\s]+/).filter(Boolean).map((x) => parseFloat(x))
    return parts.slice(0, 3)
  }
  const shadowInfo = (shadow) => {
    if (!shadow || shadow === 'none') return { none: true }
    const nums = (String(shadow).match(/-?\\d*\\.?\\d+px/g) || []).map((v) => parseFloat(v))
    return {
      none: false,
      inset: /inset/.test(shadow),
      offsets: nums.slice(0, 4),
      color: rgbOf(shadow),
    }
  }
  /** 解析 --pf-line 的实际色值（token 是 #hex，computed box-shadow 里是 rgb()）。 */
  const resolveLineColor = () => {
    const probe = document.createElement('span')
    probe.style.cssText = 'position:absolute;left:-9999px;top:0;display:block;color:var(--pf-line)'
    document.body.appendChild(probe)
    const color = getComputedStyle(probe).color
    probe.remove()
    return { css: color, rgb: rgbOf(color) }
  }

  const scanFrozen = (spec) => {
    const t = findTable(spec)
    if (!t) return { found: false }
    const tableLabel = t.getAttribute('aria-label') || ''
    // 滚动容器与票 01 冒烟同口径：从 table 往上找第一个 overflowX ∈ {auto, scroll}。
    let scroller = t.parentElement
    while (scroller && scroller !== document.body
      && !/auto|scroll/.test(getComputedStyle(scroller).overflowX)) {
      scroller = scroller.parentElement
    }
    if (!scroller || scroller === document.body) scroller = t.parentElement
    if (!scroller) return { found: false, reason: '找不到滚动容器' }

    const headRow = t.tHead && t.tHead.rows[0] ? t.tHead.rows[0] : null
    const rows = [...t.querySelectorAll('tbody tr')]
    if (!headRow) return { found: true, declared: false, tableLabel, reason: '无表头行', violations: [
      { check: 'A7', reason: '无表头行，冻结列不可测' },
    ] }
    const headCount = headRow.cells.length
    const bodyRows = rows.filter((r) => r.cells.length === headCount)
    const info = (el) => {
      const cs = getComputedStyle(el)
      const r = el.getBoundingClientRect()
      return {
        position: cs.position, left: cs.left, right: cs.right, z: cs.zIndex,
        bg: cs.backgroundColor, shadow: cs.boxShadow,
        rect: { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height },
      }
    }
    const headInfos = [...headRow.cells].map(info)
    const leftFrozen = headInfos
      .map((c, i) => (c.position === 'sticky' && c.left !== 'auto' ? { i, offset: num(c.left) } : null))
      .filter(Boolean)
    const rightFrozen = headInfos
      .map((c, i) => (c.position === 'sticky' && c.right !== 'auto' ? { i } : null))
      .filter(Boolean)
    const declared = leftFrozen.length > 0
    const leftOuter = leftFrozen.length ? leftFrozen.reduce((a, b) => (b.offset < a.offset ? b : a)) : null
    const leftBoundary = leftFrozen.length ? leftFrozen.reduce((a, b) => (b.offset > a.offset ? b : a)) : null
    const rightEdgeCell = rightFrozen.length ? rightFrozen[rightFrozen.length - 1] : null
    const line = resolveLineColor()
    const violations = []

    if (!declared) {
      // 未声明冻结列：如实判 A7–A10 全部不成立（不是「不适用」）。
      for (const check of ['A7', 'A8', 'A9', 'A10']) {
        violations.push({ check, reason: '该表未声明冻结列（表头首列 position ≠ sticky）' })
      }
    }

    const maxScroll = Math.max(0, scroller.scrollWidth - scroller.clientWidth)
    const prevScroll = scroller.scrollLeft
    scroller.scrollIntoView({ block: 'center', inline: 'nearest' })
    const padLeft = () => scroller.getBoundingClientRect().left + scroller.clientLeft
    const padRight = () => padLeft() + scroller.clientWidth
    const firstRow = bodyRows[0] || null
    const cellRect = (row, idx) => (row && row.cells[idx] ? info(row.cells[idx]) : null)

    // ---- A7 贴边（最右 / 最左两个滚位） ------------------------------------
    let leftAtMax = null
    let rightAtMax = null
    let leftAtZero = null
    let rightFrozenAtZero = null
    if (declared && firstRow && leftOuter) {
      scroller.scrollLeft = maxScroll
      const l = cellRect(firstRow, leftOuter.i)
      leftAtMax = r2(l.rect.left - padLeft())
      if (rightEdgeCell) {
        const rr = cellRect(firstRow, rightEdgeCell.i)
        rightAtMax = r2(padRight() - rr.rect.right)
      }
      scroller.scrollLeft = 0
      const l0 = cellRect(firstRow, leftOuter.i)
      leftAtZero = r2(l0.rect.left - padLeft())
      if (rightEdgeCell) {
        const r0 = cellRect(firstRow, rightEdgeCell.i)
        rightFrozenAtZero = r2(padRight() - r0.rect.right)
      }
      if (Math.abs(leftAtMax) > TOL) {
        violations.push({ check: 'A7', side: 'left', at: 'scroll=max', delta: leftAtMax, reason: '横滚到最右时首列未贴容器左缘' })
      }
      if (rightEdgeCell && Math.abs(rightAtMax) > TOL) {
        violations.push({ check: 'A7', side: 'right', at: 'scroll=max', delta: rightAtMax, reason: '操作列右侧未贴容器' })
      }
      if (Math.abs(leftAtZero) > TOL) {
        violations.push({ check: 'A7', side: 'left', at: 'scroll=0', delta: leftAtZero, reason: '横滚到最左时首列未贴容器' })
      }
      if (rightEdgeCell && Math.abs(rightFrozenAtZero) > TOL) {
        violations.push({ check: 'A7', side: 'right', at: 'scroll=0', delta: rightFrozenAtZero, reason: '操作列未贴容器右缘' })
      }
    }

    // ---- A8 不透明 + 不透字 -----------------------------------------------
    const bgSamples = []
    let hit = null
    if (declared) {
      const frozenIdx = [...leftFrozen.map((c) => c.i), ...rightFrozen.map((c) => c.i)]
      const bodies = bodyRows.length > 6 ? [...bodyRows.slice(0, 4), ...bodyRows.slice(-2)] : bodyRows
      for (const row of bodies) {
        for (const idx of frozenIdx) {
          const ci = cellRect(row, idx)
          if (!ci) continue
          const alpha = alphaOf(ci.bg)
          bgSamples.push({ kind: 'td', index: idx, bg: ci.bg, alpha })
          if (alpha < 0.999) {
            violations.push({
              check: 'A8', kind: 'td', index: idx, bg: ci.bg,
              reason: '表体冻结格背景半透/透明（邻列文字会透出）',
            })
          }
        }
      }
      for (const cell of headRow.cells) {
        const ci = info(cell)
        const isFrozen = (ci.position === 'sticky' && (ci.left !== 'auto' || ci.right !== 'auto'))
        if (!isFrozen) continue
        const alpha = alphaOf(ci.bg)
        bgSamples.push({ kind: 'th', bg: ci.bg, alpha })
        if (alpha < 0.999) {
          violations.push({ check: 'A8', kind: 'th', bg: ci.bg, reason: '表头冻结格背景半透/透明' })
        }
      }
      // 透字实测：左冻结格在 scroll=max 时被后面列覆盖，右冻结格在 scroll=0 时被前面列覆盖。
      const probeHit = (row, idx, atMax) => {
        if (!row || idx == null) return null
        scroller.scrollLeft = atMax ? maxScroll : 0
        const ci = cellRect(row, idx)
        const x = atMax ? ci.rect.right - 3 : ci.rect.left + 3
        const y = ci.rect.top + Math.min(ci.rect.height / 2, 18)
        const inView = y > 0 && y < window.innerHeight && x > 0 && x < window.innerWidth
        if (!inView) return { skipped: '冻结格不在视口内（无法 elementFromPoint）', x: r2(x), y: r2(y) }
        const el = document.elementFromPoint(x, y)
        const cell = row.cells[idx]
        return {
          x: r2(x), y: r2(y),
          el: el ? el.tagName.toLowerCase() + (el.className ? '.' + String(el.className).split(' ')[0] : '') : null,
          inside: !!el && (el === cell || cell.contains(el)),
          text: el ? text(el).slice(0, 20) : null,
        }
      }
      hit = {
        left: probeHit(firstRow, leftOuter ? leftOuter.i : null, true),
        right: probeHit(firstRow, rightEdgeCell ? rightEdgeCell.i : null, false),
      }
      for (const side of ['left', 'right']) {
        const h = hit[side]
        if (!h || h.skipped) continue
        if (!h.inside) {
          violations.push({
            check: 'A8', side, point: { x: h.x, y: h.y }, hit: h.el, hitText: h.text,
            reason: 'elementFromPoint 打在冻结格上取到的不是冻结格（邻列透出）',
          })
        }
      }
    }

    // ---- A9 边界阴影（方向 + 色值 = --pf-line） ----------------------------
    const shadows = {}
    if (declared) {
      const boundaryCell = firstRow && leftBoundary ? cellRect(firstRow, leftBoundary.i) : null
      const rightCell = firstRow && rightEdgeCell ? cellRect(firstRow, rightEdgeCell.i) : null
      const checkShadow = (side, ci, expectedX) => {
        const sh = ci ? shadowInfo(ci.shadow) : { none: true }
        shadows[side] = { shadow: ci ? ci.shadow : null, parsed: sh, expectedX, line: line.css }
        if (!ci) return
        const bad = []
        if (sh.none) bad.push('box-shadow: none')
        else {
          if (!sh.inset) bad.push('缺 inset')
          if (!sh.offsets || sh.offsets.length < 4) bad.push('阴影偏移解析失败')
          else {
            if (Math.abs(sh.offsets[0] - expectedX) > 0.51) bad.push('x 偏移 ' + sh.offsets[0] + ' ≠ ' + expectedX)
            if (Math.abs(sh.offsets[1]) > 0.51 || Math.abs(sh.offsets[2]) > 0.51 || Math.abs(sh.offsets[3]) > 0.51) {
              bad.push('其余偏移非 0：' + JSON.stringify(sh.offsets))
            }
          }
          const same = sh.color && line.rgb && sh.color.length === 3 && line.rgb.length === 3
            && sh.color.every((v, i) => Math.abs(v - line.rgb[i]) <= 1.01)
          if (!same) bad.push('色值 ' + JSON.stringify(sh.color) + ' ≠ --pf-line ' + line.css)
        }
        if (bad.length) violations.push({ check: 'A9', side, shadow: ci.shadow, reason: bad.join('；') })
      }
      checkShadow('left', boundaryCell, -1)
      checkShadow('right', rightCell, 1)
    }

    // ---- A10 z 序 --------------------------------------------------------
    const zSamples = { thead: getComputedStyle(t.tHead).zIndex, body: [], head: [] }
    if (declared) {
      if (zSamples.thead !== '20') {
        violations.push({ check: 'A10', target: 'thead', z: zSamples.thead, reason: 'thead z-index ≠ 20' })
      }
      for (const c of leftFrozen) {
        const ci = headInfos[c.i]
        zSamples.head.push({ index: c.i, z: ci.z })
        if (ci.z !== '20') {
          violations.push({ check: 'A10', target: 'thead-frozen', index: c.i, z: ci.z, reason: '表头冻结格 z ≠ 20' })
        }
      }
      for (const c of rightFrozen) {
        const ci = headInfos[c.i]
        zSamples.head.push({ index: c.i, z: ci.z })
        if (ci.z !== '20') {
          violations.push({ check: 'A10', target: 'thead-frozen', index: c.i, z: ci.z, reason: '表头冻结格 z ≠ 20' })
        }
      }
      const frozenIdx = [...leftFrozen.map((c) => c.i), ...rightFrozen.map((c) => c.i)]
      for (const row of bodyRows) {
        for (const idx of frozenIdx) {
          const ci = cellRect(row, idx)
          if (!ci) continue
          zSamples.body.push({ index: idx, z: ci.z })
          if (ci.z !== '10') {
            violations.push({ check: 'A10', target: 'tbody-frozen', index: idx, z: ci.z, reason: '表体冻结格 z ≠ 10' })
          }
        }
      }
    }

    scroller.scrollLeft = prevScroll
    const bgAlphas = bgSamples.map((b) => b.alpha)
    return {
      found: true, declared, tableLabel,
      reason: declared ? null : '该表未声明冻结列（票 02 未在原生表上实现 D9/D10）',
      scroller: { clientWidth: scroller.clientWidth, scrollWidth: scroller.scrollWidth, maxScroll: r2(maxScroll) },
      columns: {
        headCount,
        leftFrozen: leftFrozen.map((c) => ({ index: c.i, left: c.offset })),
        rightFrozen: rightFrozen.map((c) => c.i),
        rightReason: rightEdgeCell ? null : '该表无右侧冻结列（无操作列 / stickyLastCol=false）',
      },
      edges: { leftAtMax, rightAtMax, leftAtZero, rightFrozenAtZero },
      // 只留摘要 + 前 4 条样本：矩阵是证据不是日志，全量样本会把 JSON 撑到几 MB
      backgrounds: {
        count: bgSamples.length,
        alphaMin: bgAlphas.length ? r2(Math.min(...bgAlphas)) : null,
        alphaMax: bgAlphas.length ? r2(Math.max(...bgAlphas)) : null,
        samples: bgSamples.slice(0, 4),
      },
      hit,
      shadows,
      z: {
        thead: zSamples.thead,
        head: [...new Set(zSamples.head.map((h) => h.z))],
        body: [...new Set(zSamples.body.map((b) => b.z))],
      },
      line: line.css,
      violations,
    }
  }

  /** 移动卡片：字段集合 + 卡片内溢出（A12）。 */
  const scanCards = (label) => {
    const ul = document.querySelector('ul[aria-label="' + label + '"]')
    if (!ul) return { found: false }
    const lis = [...ul.querySelectorAll(':scope > li')]
    const cards = lis.map((li) => {
      const dts = [...li.querySelectorAll('dt')].map(text)
      const dd = [...li.querySelectorAll('dd')].map((d) => text(d).slice(0, 30))
      const titleEl = li.querySelector('.text-body-strong')
      return {
        labels: dts,
        values: dd,
        title: titleEl ? text(titleEl).slice(0, 30) : null,
        over: Math.max(0, li.scrollWidth - li.clientWidth),
        scrollWidth: li.scrollWidth,
        clientWidth: li.clientWidth,
      }
    })
    return {
      found: true,
      count: cards.length,
      labels: cards.length ? cards[0].labels : [],
      titles: cards.slice(0, 3).map((c) => c.title),
      overCount: cards.filter((c) => c.over > TOL).length,
      maxOver: cards.length ? r2(Math.max(...cards.map((c) => c.over))) : 0,
      page: { scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth },
    }
  }

  return { TOL, scan, scanCards, scanFrozen, findTable, allowlist: (a) => { window.__pfWidthAllowlist = a || {} } }
})()
`

/* ------------------------------------------------------------------ 探针主体 */

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function waitTable(page, spec, timeout = 30000) {
  try {
    await page.waitForFunction(
      (s) => !!(window.__pfWidth && window.__pfWidth.findTable(s)),
      spec,
      { timeout },
    )
    return true
  } catch {
    return false
  }
}

/** 切 antd Tabs（按可见文本精确匹配 tab 标题）。 */
async function clickTab(page, label) {
  const tab = page.locator('.ant-tabs-tab', { hasText: label }).first()
  if ((await tab.count()) === 0) return false
  await tab.click()
  await sleep(350)
  return true
}

/** 点表头把某列排成倒序（首点可能是升序，点到达成目标为止）。 */
async function sortDesc(page, header) {
  for (let i = 0; i < 3; i += 1) {
    const state = await page.evaluate((h) => {
      const th = [...document.querySelectorAll('thead th')].find(
        (el) => (el.textContent || '').replace(/\s+/g, ' ').trim() === h,
      )
      return th ? th.getAttribute('aria-sort') : null
    }, header)
    if (state === 'descending') return true
    const btn = page.locator('thead th button', { hasText: header }).first()
    if ((await btn.count()) === 0) return false
    await btn.click()
    await sleep(400)
  }
  return false
}

/** 行内勾选第一行（批量修改弹窗的入口）。 */
async function selectFirstRow(page) {
  const box = page.locator('tbody tr input[type=checkbox]').first()
  if ((await box.count()) === 0) return false
  await box.check({ force: true })
  await sleep(250)
  return true
}

/** 弹窗内第一个 antd 多选下拉里挑前 N 项（批量修改表要「已选资产」才渲染）。 */
async function pickMultiSelect(page, count = 3) {
  const sel = page.locator('.ant-modal .ant-select-selector').first()
  if ((await sel.count()) === 0) return false
  await sel.click()
  await sleep(350)
  const opts = page.locator('.ant-select-dropdown:visible .ant-select-item-option')
  const total = await opts.count()
  if (total === 0) return false
  for (let i = 0; i < Math.min(total, count); i += 1) {
    await opts.nth(i).click()
    await sleep(120)
  }
  await page.keyboard.press('Escape')
  await sleep(500)
  return true
}

/** 点第 nth 个 antd Select 并选中文本匹配的选项（导出预览：账期类型=按年 才触发预览 query）。 */
async function pickAntdOption(page, optionText, nth = 0) {
  const sel = page.locator('.ant-select-selector').nth(nth)
  if ((await sel.count()) === 0) return false
  await sel.click()
  await sleep(350)
  const opt = page.locator('.ant-select-dropdown:visible .ant-select-item-option', { hasText: optionText }).first()
  if ((await opt.count()) === 0) {
    await page.keyboard.press('Escape')
    return false
  }
  await opt.click()
  await sleep(700)
  return true
}

/** 原生 <select>（项目 SelectField 是原生 select；antd 只用在 Tabs/Modal）。 */
async function selectNative(page, selector, value) {
  const el = page.locator(selector).first()
  if ((await el.count()) === 0) return false
  await el.selectOption(value)
  await sleep(900)
  return true
}

async function clickByText(page, label) {
  const btn = page.getByRole('button', { name: label }).first()
  if ((await btn.count()) === 0) return false
  await btn.click()
  await sleep(400)
  return true
}

async function applyPrep(page, prep = []) {
  const done = []
  for (const step of prep) {
    if (step.tab) done.push({ tab: step.tab, ok: await clickTab(page, step.tab) })
    else if (step.sortDesc) done.push({ sortDesc: step.sortDesc, ok: await sortDesc(page, step.sortDesc) })
    else if (step.selectFirstRow) done.push({ selectFirstRow: true, ok: await selectFirstRow(page) })
    else if (step.clickText) done.push({ clickText: step.clickText, ok: await clickByText(page, step.clickText) })
    else if (step.pickMultiSelect) done.push({ pickMultiSelect: step.pickMultiSelect, ok: await pickMultiSelect(page, step.pickMultiSelect) })
    else if (step.antdOption) done.push({ antdOption: step.antdOption, ok: await pickAntdOption(page, step.antdOption, step.nth || 0) })
    else if (step.selectNative) done.push({ selectNative: step.selectNative, ok: await selectNative(page, step.selectNative.selector, step.selectNative.value) })
  }
  return done
}

;(async () => {
  if (SHOT_DIR) fs.mkdirSync(SHOT_DIR, { recursive: true })
  const groupFilter = process.env.TW_GROUPS
    ? process.env.TW_GROUPS.split(',').map((s) => s.trim()).filter(Boolean)
    : null
  const groups = groupFilter ? GROUPS.filter((g) => groupFilter.includes(g.id)) : GROUPS
  console.error(`[probe] TW_GROUPS=${JSON.stringify(process.env.TW_GROUPS ?? null)} → 生效列组 ${groups.length}/${GROUPS.length}`)

  const browser = await PW.chromium.launch({ headless: true })
  const matrix = []
  const groupMeta = []
  const checks = []
  const notes = []
  const screenshots = []
  let failShots = 0

  const check = (id, name, pass, expected, actual) =>
    checks.push({ id, name, pass: !!pass, expected, actual })

  const shotPath = (group, vp, suffix) => path.join(SHOT_DIR, `${group}-${vp}${suffix}.png`)

  /** 按 route 归组：同一页面只导航一次，视口用 setViewportSize 切换（列宽随之重算）。 */
  const byRoute = new Map()
  for (const g of groups) {
    if (!byRoute.has(g.route)) byRoute.set(g.route, [])
    byRoute.get(g.route).push(g)
  }

  for (const [route, routeGroups] of byRoute) {
    const page = await browser.newPage({ viewport: { width: DESKTOP_WIDTHS[DESKTOP_WIDTHS.length - 1], height: DESKTOP_HEIGHT } })
    await page.addInitScript({ content: PAGE_HELPERS })
    await page.addInitScript({ content: `window.__pfWidthAllowlist = ${JSON.stringify(ellipsisAllowlist)};` })
    const pageErrors = []
    page.on('pageerror', (e) => pageErrors.push(String(e).slice(0, 140)))
    try {
      await page.goto(BASE + route, { waitUntil: 'domcontentloaded', timeout: 120000 })
    } catch (e) {
      for (const g of routeGroups) {
        for (const w of [...DESKTOP_WIDTHS, ...MOBILE_WIDTHS]) {
          matrix.push({ group: g.id, groupName: g.name, route, viewport: w, status: 'error', reason: '导航失败: ' + String(e).slice(0, 120) })
        }
      }
      await page.close()
      continue
    }

    for (const g of routeGroups) {
      // prep 一律在 1280 视口上做：上一组收尾时可能停在 320/390，那时工具条/表头按钮点不到。
      await page.setViewportSize({ width: 1280, height: DESKTOP_HEIGHT })
      await sleep(200)
      // 排序/切 tab 之前必须先等表格出现（首屏数据未落地时点表头会点空 —— 实测 ok:false）。
      // 这里的等待只保证「点得到」，不是判据：交互型列组（弹窗表）本来就不会出现，
      // 等 45s 会把整轮拖垮 → 8s 足够首屏数据落地，出不来就走下面的 error 分支。
      if (g.prep && g.prep.length) await waitTable(page, g.table, 8000)
      const preps = await applyPrep(page, g.prep)
      if (g.prep && g.prep.length) await waitTable(page, g.table, 8000)
      const meta = {
        id: g.id, name: g.name, route, table: g.table, note: g.note || null,
        skipReason: g.skipReason || null, preps,
        optional: !!g.optional, minTableWidth: g.minTableWidth || 0,
      }
      groupMeta.push(meta)

      if (g.skipReason) {
        for (const w of [...DESKTOP_WIDTHS, ...MOBILE_WIDTHS]) {
          matrix.push({ group: g.id, groupName: g.name, route, viewport: w, status: 'skipped', reason: g.skipReason })
        }
        continue
      }

      // --- 桌面 9 视口：表格四层判据 ---
      // 首次渲染只等一次（最长 45s；交互型列组 10s），之后每视口给 5s 兜底 ——
      // 否则「表格根本没渲染」的列组会 9 视口 × 30s 地空转。
      const initialRendered = await waitTable(page, g.table, g.optional ? 10000 : 20000)
      console.error(`[probe] ${g.id}${initialRendered ? '' : '（表格未渲染 → 各视口记 error）'}`)
      for (const w of DESKTOP_WIDTHS) {
        const row = { group: g.id, groupName: g.name, route, viewport: w, mode: w >= TABLE_MIN_WIDTH ? 'table' : 'card' }
        await page.setViewportSize({ width: w, height: DESKTOP_HEIGHT })
        await sleep(220)
        if (w < TABLE_MIN_WIDTH) {
          // <768 是卡片侧（md:block 未生效），表格判据不适用；卡片断言在移动段落里跑。
          const cards = await page.evaluate((l) => (l ? window.__pfWidth.scanCards(l) : { found: false }), g.table.label || null)
          matrix.push({ ...row, status: 'info-card-mode', cards: cards.found ? { count: cards.count, maxOver: cards.maxOver } : null })
          continue
        }
        if (g.minTableWidth && w < g.minTableWidth) {
          matrix.push({ ...row, status: 'skipped', reason: `spec D8：该表 ${g.minTableWidth}px 起才出表` })
          continue
        }
        const present = initialRendered ? await waitTable(page, g.table, 3000) : false
        if (!present) {
          matrix.push({
            ...row, status: 'error',
            reason: initialRendered ? '该视口下表格消失' : '表格未渲染（无数据或交互未生效）',
          })
          continue
        }
        await page.evaluate(() => document.fonts.ready)
        await sleep(120)
        const scan = await page.evaluate((spec) => window.__pfWidth.scan(spec), g.table)
        // A7–A10 冻结列：与四层判据同视口、同一次导航里测（spec §6.3 R3.7）。
        const frozen = await page.evaluate((spec) => window.__pfWidth.scanFrozen(spec), g.table)
        const frozenViolations = frozen.found
          ? (frozen.violations || [])
          : [{ check: 'A7', reason: frozen.reason || '冻结列扫描未拿到表格' }]
        const failed = scan.violations.length > 0 || frozenViolations.length > 0
        matrix.push({
          ...row,
          status: failed ? 'FAIL' : 'PASS',
          headerCount: scan.headerCount,
          headers: scan.headers,
          rowCount: scan.rowCount,
          cellCount: scan.cellCount,
          violations: scan.violations.slice(0, 80),
          violationTotal: scan.violations.length,
          truncationTruncated: scan.violations.length > 80,
          colSummary: scan.colSummary,
          scroller: scan.scroller,
          tableWidth: scan.tableWidth,
          containerWidth: scan.containerWidth,
          pageScroll: scan.page,
          frozen,
          frozenViolations: frozenViolations.slice(0, 40),
          frozenViolationTotal: frozenViolations.length,
        })
        if (failed && failShots < MAX_FAIL_SHOTS && SHOT_DIR) {
          const file = shotPath(g.id, w, '-fail')
          try {
            const box = await page.evaluate((spec) => {
              const t = window.__pfWidth.findTable(spec)
              const c = t && t.parentElement && t.parentElement.parentElement
              if (!c) return null
              c.scrollIntoView({ block: 'center', inline: 'nearest' })
              const r = c.getBoundingClientRect()
              const x = Math.max(0, r.x)
              const y = Math.max(0, r.y)
              const width = Math.min(r.width, window.innerWidth - x)
              const height = Math.min(r.height, Math.min(700, window.innerHeight - y))
              if (width < 4 || height < 4) return null
              return { x, y, width, height }
            }, g.table)
            if (box) {
              await page.screenshot({ path: file, clip: box })
              screenshots.push(relToRepo(file))
              failShots += 1
            }
          } catch (e) {
            notes.push(`${g.id}@${w} 截图失败：${String(e).slice(0, 80)}`)
          }
        }
        // 1440 / 1920 全表存档（左半边 + 滚到最右的右半边，两帧合起来是整张表）
        if ((w === 1440 || w === 1920) && SHOT_DIR) {
          try {
            const handle = await page.evaluateHandle((spec) => {
              const t = window.__pfWidth.findTable(spec)
              return t && t.parentElement ? t.parentElement : null
            }, g.table)
            const el = handle.asElement()
            if (el) {
              await el.evaluate((n) => { n.scrollLeft = 0 })
              await sleep(120)
              const f1 = shotPath(g.id, w, '-full-left')
              await el.screenshot({ path: f1 })
              screenshots.push(relToRepo(f1))
              await el.evaluate((n) => { n.scrollLeft = n.scrollWidth })
              await sleep(150)
              const f2 = shotPath(g.id, w, '-full-right')
              await el.screenshot({ path: f2 })
              screenshots.push(relToRepo(f2))
            }
          } catch (e) {
            notes.push(`${g.id}@${w} 存档图失败：${String(e).slice(0, 80)}`)
          }
        }
      }

      // --- 移动 2 视口：卡片字段集合 + 卡片内溢出（A12；D8 例外表不出卡片，跳过） ---
      if (g.table.label && g.id !== 'allocation-analysis') {
        for (const w of MOBILE_WIDTHS) {
          await page.setViewportSize({ width: w, height: MOBILE_HEIGHT })
          await sleep(220)
          const cards = await page.evaluate((l) => window.__pfWidth.scanCards(l), g.table.label)
          if (!cards.found) {
            matrix.push({ group: g.id, groupName: g.name, route, viewport: w, mode: 'card', status: 'error', reason: '卡片列表未渲染' })
            continue
          }
          const desktopHeaders = (matrix.find((m) => m.group === g.id && m.viewport === 1920 && m.headers) || {}).headers || []
          const labelSet = new Set(cards.labels)
          // 只做**集合对照**，不在这里判 A12：桌面列名与卡片 dt 标签本来就不一定同名
          // （DataTable 用 column.mobile.label 覆盖，如「机构 / 渠道」→「机构」；
          //  role=title 走卡片标题、role=value 走右上金额块、都无 dt）。A12 口径待票 06 裁决，
          //  这里只把实测集合与差集写进矩阵，供裁决时直接比对。
          const notInCard = desktopHeaders.filter((h) => !labelSet.has(h))
          matrix.push({
            group: g.id, groupName: g.name, route, viewport: w, mode: 'card', status: 'INFO-card',
            cardCount: cards.count,
            cardLabels: cards.labels,
            cardTitles: cards.titles,
            desktopHeaders,
            labelsNotInCard: notInCard,
            cardFieldCount: cards.labels.length + 1 + (cards.titles.some(Boolean) ? 1 : 0),
            desktopColumnCount: desktopHeaders.length,
            cardMaxOver: cards.maxOver,
            cardOverCount: cards.overCount,
            pageScroll: cards.page,
            note: 'A12 字段集合口径待票 06 裁决；此处只出实测集合与差集，卡片内溢出按 1px 判 FAIL',
          })
        }
      }
    }
    if (pageErrors.length) notes.push(`${route} 页面报错 ${pageErrors.length} 条：${pageErrors[0]}`)
    await page.close()
  }

  // --- 汇总：矩阵断言 + 已知缺陷命中表 ---
  const tableRows = matrix.filter((m) => m.mode === 'table')
  const failedRows = tableRows.filter((m) => m.status === 'FAIL')
  const errorRows = tableRows.filter((m) => m.status === 'error')

  for (const g of groupMeta) {
    const rows = tableRows.filter((m) => m.group === g.id)
    const vps = rows.map((r) => r.viewport)
    const detail = {
      fail: rows.filter((r) => r.status === 'FAIL').map((r) => r.viewport),
      error: rows.filter((r) => r.status === 'error').map((r) => r.viewport),
      skipped: rows.filter((r) => r.status === 'skipped').map((r) => r.viewport),
    }
    if (g.optional) {
      // 交互型列组（弹窗/上传预览）：出不来只记 INFO（矩阵里仍有 error 原因），出了表才判。
      const rendered = rows.filter((r) => r.status === 'PASS' || r.status === 'FAIL')
      if (rendered.length === 0) {
        notes.push(`${g.id}：交互未生效，本列组未测（矩阵记 error/skipped 原因）`)
        continue
      }
    }
    const rendered = rows.filter((r) => r.status === 'PASS' || r.status === 'FAIL')
    if (detail.error.length) notes.push(`${g.id}：${detail.error.length} 个视口表格未渲染（矩阵记 error 原因）`)
    check(
      `TW-${g.id}`,
      `${g.name}：桌面 ${rendered.length} 个视口下四层判据零命中`,
      rendered.length > 0 && detail.fail.length === 0,
      { viewports: vps, allPass: true },
      detail,
    )
    // A7–A10 冻结列四件套（spec §6.3 R3.7：验收面 = 全部出表列组）
    const frozenRows = rows.filter((r) => r.frozen && r.frozen.found)
    if (frozenRows.length) {
      const bad = frozenRows.filter((r) => (r.frozenViolationTotal || 0) > 0)
      const perCheck = {}
      for (const r of bad) {
        for (const v of r.frozenViolations || []) {
          perCheck[v.check] = (perCheck[v.check] || 0) + 1
        }
      }
      check(
        `TW-${g.id}-frozen`,
        `${g.name}：A7–A10 冻结列（贴边 / 不透明 / 边界色 / z 序）${frozenRows.length} 个视口全通过`,
        bad.length === 0,
        { viewports: frozenRows.map((r) => r.viewport), allPass: true },
        {
          failViewports: bad.map((r) => r.viewport),
          perCheck,
          declared: frozenRows[0].frozen.declared,
          firstViolation: bad.length ? bad[0].frozenViolations[0] : null,
        },
      )
    }
  }
  for (const g of groupMeta) {
    const rows = matrix.filter((m) => m.group === g.id && m.mode === 'card' && m.status === 'INFO-card')
    if (!rows.length) continue
    check(
      `TW-${g.id}-card`,
      `${g.name}：390/320 卡片内无溢出`,
      rows.every((r) => (r.cardMaxOver || 0) <= TOL),
      { maxOver: 0 },
      rows.map((r) => ({ vp: r.viewport, maxOver: r.cardMaxOver, overCount: r.cardOverCount })),
    )
  }

  // 已知缺陷命中（反向验证的机读证据）：跨视口取该列该判据的最大溢出量。
  const knownDefects = KNOWN_DEFECTS.map((kd) => {
    const hits = []
    for (const m of tableRows) {
      if (m.group !== kd.group || m.status !== 'FAIL') continue
      for (const v of m.violations || []) {
        if (v.col !== kd.col) continue
        if (!kd.judges.includes(v.judge)) continue
        if (kd.variant === 'liability' && !/负债/.test(v.text || '')) continue
        if (kd.variant === 'plain' && /负债/.test(v.text || '')) continue
        hits.push({ viewport: m.viewport, judge: v.judge, over: v.over, kind: v.kind, text: v.text })
      }
    }
    const best = hits.reduce((a, b) => (!a || b.over > a.over ? b : a), null)
    const detected = !!best
    const target = kd.renderOver === null ? null : kd.renderOver
    const withinTol = detected && target !== null && Math.abs(best.over - target) <= kd.tol
    return {
      id: kd.id, group: kd.group, col: kd.col,
      specOver: kd.specOver, renderOver: kd.renderOver, tol: kd.tol,
      detected, withinTol,
      measuredOver: best ? best.over : null,
      viewport: best ? best.viewport : null, judge: best ? best.judge : null,
      hits: hits.length,
      renderable: kd.renderOver !== null,
      mask: kd.mask || null,
    }
  })

  const result = {
    base: BASE,
    generatedAt: new Date().toISOString(),
    tolerance: TOL,
    judges: JUDGES,
    viewports: { desktop: DESKTOP_WIDTHS, mobile: MOBILE_WIDTHS },
    tableMinWidth: TABLE_MIN_WIDTH,
    ellipsisAllowlist,
    requestedGroups: process.env.TW_GROUPS ?? null,
    effectiveGroupIds: groups.map((g) => g.id),
    groups: groupMeta,
    matrix,
    knownDefects,
    checks,
    notes,
    screenshots,
    summary: {
      tableRows: tableRows.length,
      pass: tableRows.filter((m) => m.status === 'PASS').length,
      fail: failedRows.length,
      error: errorRows.length,
      skipped: tableRows.filter((m) => m.status === 'skipped').length,
      // A7–A10 覆盖面（列组 × 视口）：只计真的扫到表的行；FAIL 行按 frozenViolationTotal 判
      frozenRows: tableRows.filter((m) => m.frozen && m.frozen.found).length,
      frozenPass: tableRows.filter((m) => m.frozen && m.frozen.found && !(m.frozenViolationTotal > 0)).length,
      frozenFail: tableRows.filter((m) => (m.frozenViolationTotal || 0) > 0).length,
      frozenGroups: [...new Set(tableRows.filter((m) => m.frozen && m.frozen.found).map((m) => m.group))].length,
      frozenWidthFailRows: tableRows.filter((m) => m.frozen && m.frozen.found && m.violationTotal > 0).length,
      frozenUndeclaredRows: tableRows.filter((m) => m.frozen && m.frozen.found && !m.frozen.declared).length,
      knownDefectsDetected: knownDefects.filter((k) => k.detected).length,
      knownDefectsTotal: knownDefects.length,
      knownDefectsWithinTol: knownDefects.filter((k) => k.withinTol).length,
    },
  }

  if (OUT_JSON) {
    fs.mkdirSync(path.dirname(OUT_JSON), { recursive: true })
    fs.writeFileSync(OUT_JSON, JSON.stringify(result, null, 2) + '\n')
    result.matrixPath = relToRepo(OUT_JSON)
  }

  await browser.close()

  const failedChecks = checks.filter((c) => !c.pass)
  // 密集摘要：全量矩阵在文件里，PROBE_RESULT 只带机读结论，避免一行几 MB。
  const compact = {
    base: BASE,
    matrixPath: result.matrixPath || null,
    summary: result.summary,
    failedGroups: failedChecks.map((c) => c.id),
    knownDefects: knownDefects.map((k) => `${k.id}:${k.detected ? (k.withinTol ? 'hit' : 'hit±tol') : 'MISS'}=${k.measuredOver}`),
    notes,
  }
  console.log('PROBE_RESULT ' + JSON.stringify(compact))
  process.exit(failedChecks.length === 0 ? 0 : 1)
})().catch((e) => {
  console.error('FATAL', e)
  process.exit(2)
})