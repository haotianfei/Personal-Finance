'use client'

import { Suspense, useEffect, useMemo, useState } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  AutoComplete,
  Button,
  Card,
  Collapse,
  Drawer,
  Dropdown,
  Empty,
  Form,
  Image,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ArrowDown, ArrowUp, ChevronDown, Pencil, Plus, Search, Trash2, UploadCloud, X } from 'lucide-react'
import { api } from '@/lib/api'
import { AUDIT_REASON_REQUIRED_TEXT, isAuditReasonRequired } from '@/lib/api'
import type { AuditReasonInput } from '@/lib/api'
import type { SourceGroup } from '@/types'
import { PageHeader } from '@/components/layout/PageHeader'
import { ConfirmDialog, InlineError } from '@/components/ui'
import { isReasonMissing } from '@/components/ui/confirmGate'
import {
  AuditReasonField,
  toAuditReasonInput,
  useAuditReasonDraft,
} from '@/components/records/AuditReasonField'

const { Text } = Typography

const SLUG_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9-_]*$/
const SLUG_PATTERN_MSG = '仅限字母、数字、- 和 _，且不以符号开头'

// ---------------------------------------------------------------------------
// rules JSON 展示助手（校正规则 + 解析规则同住 SourceGroup.rules 一列）
//
// 后端存储形状：{rename:[…], ignore:[…], exclude:[…], layout?, amount_col?,
// amount_header?, ignore_words?, …旧键}（JSON 文本）。条目形态：ignore/exclude
// 为 "子串"（旧）或 {match, note?}；rename 恒为 {match, replace, note?} dict
// （纯字符串 rename 后端不消费，视为死规则）。
//
// 写入路径：全部走服务端原子接口（按 slug/id 定位单条，事务内改写 rules JSON，
// 返回更新后的 SourceGroup）——新增/编辑/删除/swap 走 correction-rules 族，
// 解析规则走 PUT parse-rules，示例图走 guide-image 上传。前端不再做整份
// PUT JSON 手术；此处的容错解析仅用于**展示**（含坏 JSON 组的只读回落）。
// ---------------------------------------------------------------------------

type RuleType = 'rename' | 'ignore' | 'exclude'

interface RuleFamilyMeta {
  label: string
  tagColor?: string
  hint: string
  addMsg: string
  delTitle: string
  delDesc: string
  delMsg: string
  matchLabel: string
  matchPlaceholder: string
  matchExtra?: string
}

const RULE_META: Record<RuleType, RuleFamilyMeta> = {
  rename: {
    label: '改名',
    tagColor: 'blue',
    hint: '来源名包含 match 即替换为 replace（子串匹配）；按列表顺序依次生效，前条输出可被后条继续改写',
    addMsg: '已添加改名规则',
    delTitle: '删除这条「改名」规则？',
    delDesc: '删除后来源名不再按此规则改写',
    delMsg: '已删除，名称不再按此规则改写',
    matchLabel: '匹配子串',
    matchPlaceholder: '来源名中包含的子串',
  },
  ignore: {
    label: '忽略',
    hint: '来源名包含 match 子串的行整行丢弃（子串匹配）',
    addMsg: '已添加忽略规则',
    delTitle: '删除这条「忽略」规则？',
    delDesc: '删除后命中该子串的噪声行将重新出现在识别结果',
    delMsg: '已删除，命中该子串的行将重新出现在识别结果',
    matchLabel: '匹配子串',
    matchPlaceholder: '来源名中包含的子串',
  },
  exclude: {
    label: '不录入',
    tagColor: 'amber',
    hint: '按资产全名精确相等匹配（非子串），命中资产不再进入待录入列表',
    addMsg: '已添加不录入规则，该资产不再出现在待录入列表',
    delTitle: '删除这条「不录入」规则？',
    delDesc: '删除后该资产会重新出现在截图录入的未识别列表',
    delMsg: '已删除，该资产将重新出现在未识别列表',
    matchLabel: '资产全名',
    matchPlaceholder: '资产全名（精确一致）',
    matchExtra: '需与资产全名完全一致（精确匹配，非子串）；候选来自现有资产，按本组「资产名前缀」过滤',
  },
}

// 后端 organize_blocks 实际消费的解析键（screenshot_service.py）；不在此列
// 也不在 rename/ignore/exclude 的键视为「其他历史键」，只读展示、保存原样保留
const PARSE_KEYS = ['layout', 'amount_col', 'amount_header', 'ignore_words'] as const
const KNOWN_RULE_KEYS = ['rename', 'ignore', 'exclude', ...PARSE_KEYS]
// layout 已知枚举（不含「自动检测」= 不设 layout 键）
const LAYOUT_VALUES = ['horizontal', 'vertical', 'multi_column', 'card']

interface RuleEntry {
  match: string
  replace?: string
  note?: string
}

// 展示条目 = 规范化 RuleEntry + 原始数组下标（编辑/删除/移动按 idx 精确定位；
// 规范化时跳过的畸形条目留在 raw 中不动，idx 保证不受影响）
interface DisplayRule extends RuleEntry {
  idx: number
}

interface ParsedRules {
  rename: DisplayRule[]
  ignore: DisplayRule[]
  exclude: DisplayRule[]
  raw: Record<string, unknown>
}

// 取条目的 match 子串（兼容字符串/dict 双形态）
function ruleMatchOf(entry: unknown): string | null {
  if (typeof entry === 'string') return entry
  if (entry && typeof entry === 'object') {
    const m = (entry as Record<string, unknown>).match
    if (typeof m === 'string') return m
  }
  return null
}

// 容错取原始 dict：空（null/''）→ {}；坏 JSON / 非 dict → null（UI 禁用编辑防丢数据）
function rawRulesOf(rules: unknown): Record<string, unknown> | null {
  if (rules === null || rules === undefined || rules === '') return {}
  let obj: unknown = rules
  if (typeof rules === 'string') {
    try {
      obj = JSON.parse(rules)
    } catch {
      return null
    }
  }
  if (typeof obj !== 'object' || obj === null || Array.isArray(obj)) return null
  return obj as Record<string, unknown>
}

// 规范化某家族的展示列表（带原始下标）。rename 只认 dict 形态（后端
// apply_correction_rules 跳过纯字符串 rename）；replace 允许空串（语义=删除子串）。
function displayRulesOf(raw: Record<string, unknown>, type: RuleType): DisplayRule[] {
  const arr = Array.isArray(raw[type]) ? raw[type] : []
  const out: DisplayRule[] = []
  arr.forEach((e, idx) => {
    const isObj = !!e && typeof e === 'object'
    if (type === 'rename' && !isObj) return
    const match = ruleMatchOf(e)
    if (!match) return
    const d = (isObj ? e : {}) as Record<string, unknown>
    out.push({
      idx,
      match,
      ...(typeof d.replace === 'string' ? { replace: d.replace } : {}),
      ...(typeof d.note === 'string' && d.note ? { note: d.note } : {}),
    })
  })
  return out
}

function parseGroupRules(rules: unknown): ParsedRules | null {
  const raw = rawRulesOf(rules)
  if (!raw) return null
  return {
    rename: displayRulesOf(raw, 'rename'),
    ignore: displayRulesOf(raw, 'ignore'),
    exclude: displayRulesOf(raw, 'exclude'),
    raw,
  }
}

const arrOf = (raw: Record<string, unknown>, type: RuleType): unknown[] =>
  Array.isArray(raw[type]) ? raw[type] : []

// 同类型去重（对齐后端 save_correction_rule 判重语义）：ignore/exclude 比 match；
// rename 比 match+replace。selfIdx >= 0 时排除自身（编辑场景）。
function findDuplicateRule(
  raw: Record<string, unknown> | null,
  type: RuleType,
  match: string,
  replace: string | undefined,
  selfIdx: number,
): boolean {
  if (!raw) return false
  return arrOf(raw, type).some((e, i) => {
    if (i === selfIdx) return false
    if (ruleMatchOf(e) !== match) return false
    if (type !== 'rename') return true
    const r = e && typeof e === 'object' ? (e as Record<string, unknown>).replace : undefined
    return r === replace
  })
}

// trim + 去空 + 去重（tags 输入清洗）
function cleanTagList(list: string[] | undefined): string[] {
  const out: string[] = []
  for (const s of list ?? []) {
    const t = (s ?? '').trim()
    if (t && !out.includes(t)) out.push(t)
  }
  return out
}

// --- 规则写入 mutation（所有原子端点共用）：调用返回更新后 SourceGroup 的
// 服务端接口 → 用返回值回填缓存（表格 Tag / Modal 摘要 / Drawer 即时刷新）→
// invalidate 对账；错误 message 原样展示后端中文 detail（经 request() 提取） ---
interface GroupSaveVars {
  run: () => Promise<SourceGroup>
  okMsg: string
}

function useGroupSaveMutation() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ run, okMsg }: GroupSaveVars): Promise<string> => {
      const updated = await run()
      queryClient.setQueryData<SourceGroup[]>(['source-groups'], (old) =>
        old ? old.map((x) => (x.id === updated.id ? updated : x)) : old,
      )
      return okMsg
    },
    onSuccess: (okMsg) => {
      queryClient.invalidateQueries({ queryKey: ['source-groups'] })
      message.success(okMsg)
    },
    onError: (e) => message.error(e instanceof Error ? e.message : '操作失败'),
  })
}

// ---------------------------------------------------------------------------
// 规则管理 Drawer（三段：校正规则 / 解析规则 / 指引与示例图）
// group 一律取 live query 数据（父级按 id 从 groups 里找），不用打开时快照
// ---------------------------------------------------------------------------

type RulesTab = 'correction' | 'parse' | 'guide'

interface RuleFormValues {
  match: string
  replace?: string
  note?: string
}

// 行内新增/编辑表单的定位信息：edit 带原始下标 + 条目快照（回填表单值）
type RuleEditTarget =
  | { mode: 'add'; type: RuleType }
  | { mode: 'edit'; type: RuleType; idx: number; entry: DisplayRule }

function CorrectionRulesPanel({ group }: { group: SourceGroup }) {
  const queryClient = useQueryClient()
  const save = useGroupSaveMutation()
  const [ruleForm] = Form.useForm<RuleFormValues>()
  const [typeFilter, setTypeFilter] = useState<'all' | RuleType>('all')
  const [search, setSearch] = useState('')
  const [target, setTarget] = useState<RuleEditTarget | null>(null)
  /** 待删的那条规则（弹出理由框）；null = 框关着。 */
  const [ruleToDelete, setRuleToDelete] = useState<{ type: RuleType; entry: DisplayRule } | null>(null)
  /** 删除框内的提示（缺理由时出现；弹窗不关）。 */
  const [ruleDeleteError, setRuleDeleteError] = useState<string | null>(null)

  // 理由草稿按规则保留：关窗不清、换一条规则才清（改名规则还要带 replace 区分同名对）
  const [ruleDeleteReason, setRuleDeleteReason] = useAuditReasonDraft(
    ruleToDelete
      ? `${ruleToDelete.type}|${ruleToDelete.entry.match}|${ruleToDelete.entry.replace ?? ''}`
      : null,
  )
  /** 缺理由点确认时 +1：让理由控件自己显红字并回焦（与 ConfirmDialog 的门禁同一套反馈）。 */
  const [ruleDeleteNudge, setRuleDeleteNudge] = useState(0)

  const raw = useMemo(() => rawRulesOf(group.rules), [group.rules])
  const lists = useMemo(
    () => ({
      rename: raw ? displayRulesOf(raw, 'rename') : [],
      ignore: raw ? displayRulesOf(raw, 'ignore') : [],
      exclude: raw ? displayRulesOf(raw, 'exclude') : [],
    }),
    [raw],
  )
  const total = lists.rename.length + lists.ignore.length + lists.exclude.length

  // 新增/编辑「不录入」时拉资产全名候选（历史行按名去重；有前缀时服务端粗筛+客户端 startsWith 精筛）
  const prefix = group.asset_prefix ?? ''
  const excludeCandidates = useQuery({
    queryKey: ['source-group-exclude-candidates', prefix],
    enabled: target?.type === 'exclude',
    staleTime: 5 * 60_000,
    queryFn: async () => {
      const res = await api.getAssets(prefix ? { page_size: '1000', asset_name: prefix } : { page_size: '1000' })
      const names = new Set<string>()
      for (const it of res.items) {
        if (it.asset_name && (!prefix || it.asset_name.startsWith(prefix))) names.add(it.asset_name)
      }
      return [...names].slice(0, 300)
    },
  })

  // target 变化后回填行内表单（effect 在 Form 挂载提交后执行，避免 useForm 未连接告警）
  useEffect(() => {
    if (!target) return
    ruleForm.resetFields()
    if (target.mode === 'edit') {
      ruleForm.setFieldsValue({
        match: target.entry.match,
        replace: target.entry.replace ?? '',
        note: target.entry.note ?? '',
      })
    }
  }, [target, ruleForm])

  // --- 单条操作（服务端原子端点：POST 新增 / PATCH 编辑 / DELETE 删除 / swap 排序） ---
  const submitRuleForm = (type: RuleType, t: RuleEditTarget, values: RuleFormValues) => {
    const match = (values.match ?? '').trim()
    const note = (values.note ?? '').trim()
    const replace = type === 'rename' ? (values.replace ?? '').trim() : undefined
    if (type === 'rename' && !replace) return // required 校验已拦；双保险
    // 客户端查重仅作快速反馈（与后端判重同语义：ignore/exclude 比 match，rename 比 match+replace）；服务端为准
    if (findDuplicateRule(raw, type, match, replace, t.mode === 'edit' ? t.idx : -1)) {
      ruleForm.setFields([{ name: 'match', errors: ['同类型下已存在相同匹配内容的规则'] }])
      return
    }
    const slug = group.slug
    if (t.mode === 'add') {
      // 新增走截图录入页同款 POST（append-only、幂等）；note 空串后端按无备注处理
      save.mutate(
        {
          run: () => api.saveCorrectionRule(slug, { type, match, replace, note }),
          okMsg: RULE_META[type].addMsg,
        },
        { onSuccess: () => setTarget(null) },
      )
      return
    }
    // 编辑走 PATCH：旧身份 {match[, replace][, note]} 定位 + new_* 新值；
    // new_note 恒传（空串 = 删除备注，契约语义）
    const old = t.entry
    save.mutate(
      {
        run: () =>
          api.patchCorrectionRule(slug, {
            type,
            match: old.match,
            ...(type === 'rename' ? { replace: old.replace } : {}),
            ...(old.note ? { note: old.note } : {}),
            new_match: match,
            ...(type === 'rename' ? { new_replace: replace } : {}),
            new_note: note,
          }),
        okMsg: '规则已更新',
      },
      { onSuccess: () => setTarget(null) },
    )
  }

  // 删除走独立 mutation（不复用 useGroupSaveMutation）：缺理由要**留在框里**并给行内说明，
  // 而那个共用 mutation 的错误处理是统一 toast。
  const removeMutation = useMutation({
    mutationFn: ({ type, entry, audit }: { type: RuleType; entry: DisplayRule; audit: AuditReasonInput }) =>
      api.deleteCorrectionRule(
        group.slug,
        { type, match: entry.match, ...(type === 'rename' ? { replace: entry.replace } : {}) },
        audit,
      ),
    onSuccess: (updated, { type }) => {
      // 与 useGroupSaveMutation 同一条回填路径：Drawer 里的规则列表立刻反映
      queryClient.setQueryData<SourceGroup[]>(['source-groups'], (old) =>
        old ? old.map((x) => (x.id === updated.id ? updated : x)) : old,
      )
      queryClient.invalidateQueries({ queryKey: ['source-groups'] })
      message.success(RULE_META[type].delMsg)
      setRuleToDelete(null)
      setRuleDeleteError(null)
    },
    onError: (error) => {
      // 缺理由（422）不是「删不掉」：框和已选的理由都留着，补一句再点一次。
      if (isAuditReasonRequired(error)) {
        setRuleDeleteError(AUDIT_REASON_REQUIRED_TEXT)
        return
      }
      message.error(error instanceof Error ? error.message : '操作失败')
    },
  })

  const busy = save.isPending || removeMutation.isPending

  const closeRuleDelete = () => {
    setRuleToDelete(null)
    setRuleDeleteError(null)
  }

  // 缺理由不发请求：与 ConfirmDialog 的门禁同一条判定、同一处反馈
  //（理由控件自己显红字 + 焦点回下拉），区别只是确认按钮由本面板画。
  const confirmRuleDelete = () => {
    if (!ruleToDelete) return
    if (isReasonMissing(ruleDeleteReason.reason)) {
      setRuleDeleteNudge((n) => n + 1)
      return
    }
    removeMutation.mutate({
      type: ruleToDelete.type,
      entry: ruleToDelete.entry,
      audit: toAuditReasonInput(ruleDeleteReason),
    })
  }

  // rename 排序：服务端按身份交换 a/b 两条的位置（列表顺序即后端 apply 顺序）；
  // a/b 取显示列表中相邻两条，中间夹着被规范化跳过的畸形条目也不影响语义
  const swapRules = (a: DisplayRule, b: DisplayRule) => {
    save.mutate({
      run: () =>
        api.swapCorrectionRules(group.slug, {
          type: 'rename',
          a: { match: a.match, replace: a.replace },
          b: { match: b.match, replace: b.replace },
        }),
      okMsg: '已调整顺序',
    })
  }

  const startAdd = (type: RuleType) => {
    setTarget({ mode: 'add', type })
    setTypeFilter(type) // 保证新表单所在分区一定可见
  }

  // --- 渲染 ---
  if (!raw) {
    return (
      <Alert
        type="error"
        showIcon
        title="rules 字段无法解析为 JSON"
        description={
          <div className="text-xs">
            <div>为避免误覆盖丢数据，已禁用规则编辑。原始内容（保存于 rules 列，原样未动）：</div>
            <pre className="mt-1.5 max-h-40 overflow-auto rounded bg-slate-100 p-2 text-xs text-slate-600">
              {String(group.rules)}
            </pre>
          </div>
        }
      />
    )
  }

  const q = search.trim().toLowerCase()
  const matchesSearch = (e: DisplayRule) =>
    !q || [e.match, e.replace, e.note].some((s) => s && s.toLowerCase().includes(q))

  const renderRuleForm = (type: RuleType, t: RuleEditTarget) => {
    const meta = RULE_META[type]
    const required = [{ required: true, whitespace: true, message: '不能为空' }]
    return (
      <div className="my-1.5 rounded-lg border border-blue-200 bg-blue-50/40 px-3 py-2.5">
        <div className="mb-2 text-xs font-medium text-slate-500">
          {t.mode === 'add' ? '新增' : '编辑'}「{meta.label}」规则
        </div>
        <Form form={ruleForm} layout="vertical" size="small" onFinish={(v) => submitRuleForm(type, t, v)}>
          {type === 'rename' ? (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-3">
              <Form.Item name="match" label={meta.matchLabel} rules={required}>
                <Input placeholder={meta.matchPlaceholder} />
              </Form.Item>
              <Form.Item name="replace" label="替换为" rules={required}>
                <Input placeholder="替换后的子串" />
              </Form.Item>
            </div>
          ) : (
            <Form.Item name="match" label={meta.matchLabel} rules={required} extra={meta.matchExtra}>
              {type === 'exclude' ? (
                <AutoComplete
                  placeholder={meta.matchPlaceholder}
                  options={(excludeCandidates.data ?? []).map((n) => ({ value: n }))}
                  filterOption={(input, option) =>
                    String(option?.value ?? '').toLowerCase().includes(input.trim().toLowerCase())
                  }
                  notFoundContent={excludeCandidates.isFetching ? '加载中…' : null}
                />
              ) : (
                <Input placeholder={meta.matchPlaceholder} />
              )}
            </Form.Item>
          )}
          <Form.Item name="note" label="备注（仅展示，不参与匹配）">
            <Input placeholder="可选" maxLength={100} />
          </Form.Item>
          <div className="flex justify-end gap-2">
            <Button size="small" onClick={() => setTarget(null)}>
              取消
            </Button>
            <Button size="small" type="primary" htmlType="submit" loading={busy}>
              保存
            </Button>
          </div>
        </Form>
      </div>
    )
  }

  const renderSection = (type: RuleType) => {
    if (typeFilter !== 'all' && typeFilter !== type) return null
    const meta = RULE_META[type]
    const all = lists[type]
    const editIdxHere = target?.mode === 'edit' && target.type === type ? target.idx : -1
    // 正在编辑的行始终可见（不受搜索过滤影响，避免表单随行消失）
    const shown = all.filter((e) => e.idx === editIdxHere || matchesSearch(e))
    const addingHere = target?.mode === 'add' && target.type === type
    if (!addingHere && shown.length === 0) return null
    return (
      <div key={type} className="mb-3">
        <div className="mb-1 flex items-baseline gap-2">
          <Tag color={meta.tagColor} className="!mr-0 !text-meta !leading-4 shrink-0">
            {meta.label}
          </Tag>
          <span className="min-w-0 text-meta leading-4 text-muted">{meta.hint}</span>
          <span className="ml-auto shrink-0 text-meta text-muted">{all.length} 条</span>
        </div>
        {addingHere && target && renderRuleForm(type, target)}
        <ul className="space-y-0.5">
          {shown.map((e, pos) =>
            target?.mode === 'edit' && target.type === type && target.idx === e.idx ? (
              <li key={`edit-${e.idx}`} className="list-none">
                {renderRuleForm(type, target)}
              </li>
            ) : (
              <li key={`${type}-${e.idx}`} className="flex min-w-0 items-center gap-1.5 py-0.5 text-xs text-slate-600">
                {type === 'rename' && (
                  <span className="w-4 shrink-0 text-right text-meta text-muted">{pos + 1}</span>
                )}
                <span className="truncate" title={type === 'rename' ? `${e.match} → ${e.replace ?? ''}` : e.match}>
                  {e.match}
                  {type === 'rename' && <span className="text-muted"> → {e.replace || '（空）'}</span>}
                </span>
                {e.note && (
                  <span className="max-w-[30%] shrink-0 truncate text-muted" title={e.note}>
                    · {e.note}
                  </span>
                )}
                <span className="ml-auto flex shrink-0 items-center">
                  {type === 'rename' && (
                    <>
                      <Button
                        size="small"
                        type="text"
                        className="!px-1 !text-muted hover:!text-slate-600"
                        disabled={busy || pos === 0}
                        title="上移（更早生效）"
                        onClick={() => swapRules(shown[pos - 1], e)}
                      >
                        <ArrowUp size={12} />
                      </Button>
                      <Button
                        size="small"
                        type="text"
                        className="!px-1 !text-muted hover:!text-slate-600"
                        disabled={busy || pos === shown.length - 1}
                        title="下移（更晚生效）"
                        onClick={() => swapRules(e, shown[pos + 1])}
                      >
                        <ArrowDown size={12} />
                      </Button>
                    </>
                  )}
                  <Button
                    size="small"
                    type="text"
                    className="!px-1 !text-muted hover:!text-blue-500"
                    disabled={busy}
                    title="编辑"
                    onClick={() => setTarget({ mode: 'edit', type, idx: e.idx, entry: e })}
                  >
                    <Pencil size={12} />
                  </Button>
                  <Button
                    size="small"
                    type="text"
                    className="!px-1 !text-slate-300 hover:!text-red-500"
                    disabled={busy}
                    title="删除"
                    onClick={() => setRuleToDelete({ type, entry: e })}
                  >
                    <X size={12} />
                  </Button>
                </span>
              </li>
            ),
          )}
        </ul>
      </div>
    )
  }

  const sections = (['rename', 'ignore', 'exclude'] as RuleType[]).map(renderSection).filter(Boolean)

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Segmented
          size="small"
          value={typeFilter}
          onChange={(v) => {
            setTypeFilter(v as 'all' | RuleType)
            // 切换分区可能隐藏正在编辑的行，收起编辑表单防「表单悬空」
            setTarget((t) => (t?.mode === 'edit' ? null : t))
          }}
          options={[
            { label: `全部 ${total}`, value: 'all' },
            { label: `改名 ${lists.rename.length}`, value: 'rename' },
            { label: `忽略 ${lists.ignore.length}`, value: 'ignore' },
            { label: `不录入 ${lists.exclude.length}`, value: 'exclude' },
          ]}
        />
        <Input
          size="small"
          allowClear
          className="!w-44"
          prefix={<Search size={12} className="text-muted" />}
          placeholder="搜索匹配/备注"
          value={search}
          onChange={(ev) => setSearch(ev.target.value)}
        />
        <Dropdown.Button
          size="small"
          type="primary"
          className="ml-auto"
          disabled={busy}
          icon={<ChevronDown size={12} />}
          onClick={() => startAdd(typeFilter === 'all' ? 'rename' : typeFilter)}
          menu={{
            items: [
              { key: 'rename', label: '改名规则' },
              { key: 'ignore', label: '忽略规则' },
              { key: 'exclude', label: '不录入规则' },
            ],
            onClick: ({ key }) => startAdd(key as RuleType),
          }}
        >
          <Plus size={12} className="mr-1 inline-block align-[-1px]" />
          新增规则
        </Dropdown.Button>
      </div>

      {total === 0 && !target ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          className="py-6"
          description={
            <span className="text-xs text-muted">
              暂无校正规则。规则通常产生于截图录入页的「存为规则 / 不录入」，也可点击右上角手动新增
            </span>
          }
        />
      ) : sections.length > 0 ? (
        sections
      ) : (
        <div className="py-4 text-center text-xs text-muted">无匹配规则</div>
      )}

      {/* 删除单条规则：用 antd Modal，而不是页面上统一的 ConfirmDialog。
          本面板常年活在 antd Drawer 里（v6 的 Drawer 默认 `focusable.trap` 锁焦点、
          容器层从 1000 起）：自绘弹层是 body 门 + `z-modal`(50)，既被抽屉压住，
          也抢不到焦点——抽屉的 focusin 锁会把焦点抢回面板，理由输入框根本打不进字。
          antd Modal 与抽屉共用同一套层级/焦点/Esc 栈：自动叠在抽屉（1100）之上，
          Esc 只关最上层，关掉后焦点回抽屉。理由必填仍是同一条判定（isReasonMissing）
          与同一处反馈（nudgeToken → 理由控件显红字 + 回焦），只是确认按钮由这里画。 */}
      <Modal
        open={!!ruleToDelete}
        title={ruleToDelete ? RULE_META[ruleToDelete.type].delTitle : '删除规则？'}
        okText="删除规则"
        okButtonProps={{ danger: true }}
        cancelText="取消"
        width={480}
        confirmLoading={removeMutation.isPending}
        cancelButtonProps={{ disabled: removeMutation.isPending }}
        destroyOnHidden
        onOk={confirmRuleDelete}
        onCancel={closeRuleDelete}
      >
        <div className="space-y-3">
          {ruleToDelete && (
            <p className="text-body text-secondary">{RULE_META[ruleToDelete.type].delDesc}</p>
          )}
          {ruleDeleteError && <InlineError>{ruleDeleteError}</InlineError>}
          <AuditReasonField
            label="为什么删"
            value={ruleDeleteReason}
            onChange={setRuleDeleteReason}
            nudgeToken={ruleDeleteNudge}
            hint="删除会留下谁在何时删了哪一条规则。"
          />
        </div>
      </Modal>
    </div>
  )
}

// --- 解析规则：结构化表单（仅动四个已知键；校正规则与其他历史键原样保留） ---

interface ParseFormValues {
  layout: string // '' = 自动检测（保存时删除 layout 键）
  amount_col: number
  amount_header: string[]
  ignore_words: string[]
}

const LAYOUT_OPTIONS = [
  { value: '', label: '自动检测（默认，按行签名判定）' },
  { value: 'horizontal', label: '横排 —— 同一行左名右金，一行一条' },
  { value: 'vertical', label: '竖排 —— 名称行 + 金额行上下结构，靠表头关键词定位' },
  { value: 'multi_column', label: '多列表格 —— 名称 + 多列金额，取第 N 列' },
  { value: 'card', label: '卡片 —— 上行多名称、下行多金额，按横向就近配对' },
]

function initParseForm(raw: Record<string, unknown>): ParseFormValues {
  const strArr = (v: unknown): string[] =>
    Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []
  const col = Number(raw.amount_col)
  return {
    // 未知枚举值原样进表单（Select 直接显示原值），保存时原样写回，防静默丢失
    layout: typeof raw.layout === 'string' ? raw.layout : '',
    amount_col: Number.isFinite(col) ? Math.max(0, Math.trunc(col)) : 0,
    amount_header: strArr(raw.amount_header),
    ignore_words: strArr(raw.ignore_words),
  }
}

// 既有值形态异常提示（不静默吞：显示为什么与库中不一致、保存会发生什么）
function parseAnomaliesOf(raw: Record<string, unknown>): string[] {
  const out: string[] = []
  const layout = raw.layout
  if (layout !== undefined && layout !== null) {
    if (typeof layout !== 'string') {
      out.push(`layout 不是字符串（当前 ${JSON.stringify(layout)}），已按「自动检测」显示；保存将按当前选择覆盖`)
    } else if (layout && !LAYOUT_VALUES.includes(layout)) {
      out.push(`layout 值「${layout}」不在已知枚举（horizontal / vertical / multi_column / card），未改动时保存原样写回`)
    }
  }
  if (raw.amount_col !== undefined && raw.amount_col !== null && !Number.isFinite(Number(raw.amount_col))) {
    out.push('amount_col 不是数字，已按 0 处理；保存将覆盖')
  }
  for (const k of ['amount_header', 'ignore_words'] as const) {
    const v = raw[k]
    if (v !== undefined && v !== null && !(Array.isArray(v) && v.every((x) => typeof x === 'string'))) {
      out.push(`${k} 不是字符串数组，已按空显示；保存将覆盖`)
    }
  }
  return out
}

function ParseRulesPanel({ group }: { group: SourceGroup }) {
  const [form] = Form.useForm<ParseFormValues>()
  const save = useGroupSaveMutation()
  const raw = useMemo(() => rawRulesOf(group.rules), [group.rules])
  const layoutWatch = Form.useWatch('layout', form)

  if (!raw) {
    return <Alert type="error" showIcon title="rules 字段无法解析为 JSON，解析规则编辑已禁用（原内容未改动）" />
  }

  const anomalies = parseAnomaliesOf(raw)
  const otherEntries = Object.entries(raw).filter(([k]) => !(KNOWN_RULE_KEYS as readonly string[]).includes(k))

  const submit = async () => {
    const v = await form.validateFields()
    const layoutVal = (v.layout ?? '').trim()
    const headers = cleanTagList(v.amount_header)
    const words = cleanTagList(v.ignore_words)
    const col = Math.max(0, Math.trunc(Number(v.amount_col) || 0))
    // 服务端只 merge 四键并保留未知键（含校正规则/历史键）；null/[] = 删键回落内置默认
    save.mutate({
      run: () =>
        api.updateParseRules(group.id, {
          layout: layoutVal || null,
          // amount_col 仅 multi_column 消费；其他版式传 null 删键，保持 JSON 干净
          amount_col: layoutVal === 'multi_column' ? col : null,
          amount_header: headers,
          ignore_words: words,
        }),
      okMsg: '解析规则已保存，下次截图识别生效',
    })
  }

  return (
    <div>
      <Text type="secondary" className="mb-3 block text-xs">
        解析规则决定 OCR 文本块如何组织成「名称 + 金额」行，由截图识别管线直接消费；留空字段使用后端内置默认。与校正规则互不影响。
      </Text>
      {anomalies.length > 0 && (
        <Alert
          className="mb-3"
          type="warning"
          showIcon
          title="部分既有值形态异常"
          description={
            <ul className="list-disc pl-4 text-xs">
              {anomalies.map((a) => (
                <li key={a}>{a}</li>
              ))}
            </ul>
          }
        />
      )}
      <Form form={form} layout="vertical" initialValues={initParseForm(raw)} className="max-w-md">
        <Form.Item name="layout" label="版式（layout）" extra="不指定时由后端按行签名自动判定">
          <Select options={LAYOUT_OPTIONS} />
        </Form.Item>
        <Form.Item
          name="amount_col"
          label="金额列序号（amount_col）"
          extra={
            layoutWatch === 'multi_column'
              ? '取行内第几列金额：0 = 第一列（仅统计行内出现的多列金额）'
              : '仅「多列表格」版式生效，其他版式下保存时移除'
          }
        >
          <InputNumber min={0} precision={0} className="!w-full" disabled={layoutWatch !== 'multi_column'} />
        </Form.Item>
        <Form.Item
          name="amount_header"
          label="金额表头关键词（amount_header）"
          extra="竖排/自动检测时用于定位金额行的关键词。留空 = 内置默认；填写后整体替换默认（非追加）"
        >
          <Select mode="tags" tokenSeparators={[',', '，']} open={false} suffixIcon={null} placeholder="输入后回车添加" />
        </Form.Item>
        <Form.Item
          name="ignore_words"
          label="忽略词（ignore_words）"
          extra="含这些词的 OCR 行整行丢弃（按钮文案、状态栏等噪声）。留空 = 内置默认；填写后整体替换默认（非追加）"
        >
          <Select mode="tags" tokenSeparators={[',', '，']} open={false} suffixIcon={null} placeholder="输入后回车添加" />
        </Form.Item>
        <div className="flex items-center gap-3">
          <Button type="primary" loading={save.isPending} onClick={submit}>
            保存解析规则
          </Button>
          <Text type="secondary" className="text-xs">
            仅修改以上四键，校正规则与其他历史键原样保留
          </Text>
        </div>
      </Form>
      {otherEntries.length > 0 && (
        <Collapse
          className="mt-4"
          size="small"
          items={[
            {
              key: 'other',
              label: `其他历史键（${otherEntries.length} 个，只读）`,
              children: (
                <div>
                  <pre className="max-h-48 overflow-auto rounded bg-slate-50 p-2 text-xs text-slate-600">
                    {JSON.stringify(Object.fromEntries(otherEntries), null, 2)}
                  </pre>
                  <Text type="secondary" className="mt-1 block text-xs">
                    旧 spec 键（如 name_col / total_header / amount_suffix / anchor_summary）已不被后端消费；保存解析规则或校正规则时原样保留，如需清理请通过 API 手动处理
                  </Text>
                </div>
              ),
            },
          ]}
        />
      )}
    </div>
  )
}

// --- 指引与示例图：guide 文本只在编辑弹窗改（避免双入口）；guide_image 仅此处可编辑 ---

const IMAGE_ACCEPT = '.png,.jpg,.jpeg,.webp'
const IMAGE_MIME_OK = ['image/png', 'image/jpeg', 'image/webp']
const IMAGE_MAX_BYTES = 5 * 1024 * 1024

function GuidePanel({ group }: { group: SourceGroup }) {
  const save = useGroupSaveMutation()
  const [url, setUrl] = useState(group.guide_image ?? '')
  const [uploading, setUploading] = useState(false)
  const [imgFailed, setImgFailed] = useState(false)
  const current = (group.guide_image ?? '').trim()
  const dirty = url.trim() !== current

  // guide_image 外部变化（上传/清除/保存成功回填缓存）后同步输入框并复位加载失败标记
  useEffect(() => {
    setUrl(group.guide_image ?? '')
    setImgFailed(false)
  }, [group.guide_image])

  // 上传前置校验（契约：png/jpg/jpeg/webp ≤5MB），不合格拦截并提示
  const beforeUpload = (file: File) => {
    if (!IMAGE_MIME_OK.includes(file.type)) {
      message.error('仅支持 png / jpg / jpeg / webp 图片')
      return Upload.LIST_IGNORE
    }
    if (file.size > IMAGE_MAX_BYTES) {
      message.error('图片不能超过 5MB')
      return Upload.LIST_IGNORE
    }
    return true
  }

  return (
    <div className="space-y-5">
      <div>
        <div className="mb-1 text-xs font-medium text-slate-500">录入指引（文本）</div>
        {group.guide ? (
          <div className="whitespace-pre-wrap rounded-lg border border-slate-200 bg-slate-50/60 px-3 py-2 text-xs text-slate-600">
            {group.guide}
          </div>
        ) : (
          <Text type="secondary" className="text-xs">
            未填写
          </Text>
        )}
        <div className="mt-1 text-xs text-muted">
          指引文本请在「编辑来源组」弹窗中修改（保持单一编辑入口），展示于截图录入页
        </div>
      </div>
      <div>
        <div className="mb-1 text-xs font-medium text-slate-500">示例图（guide_image）</div>
        {current ? (
          imgFailed ? (
            <div className="mb-2 max-w-md rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
              图片加载失败（静态服务未就绪或地址无效）：<Text code>{current}</Text>
            </div>
          ) : (
            <div className="mb-2">
              {/* 根相对 URL（/api/static/…）经 next rewrite 同源转发，外部 http URL 直接渲染 */}
              <Image
                src={current}
                alt="录入示例图"
                style={{ maxHeight: 240, objectFit: 'contain' }}
                className="rounded-lg border border-slate-200"
                onError={() => setImgFailed(true)}
              />
              <div className="mt-1">
                <Text code className="!text-xs">
                  {current}
                </Text>
              </div>
            </div>
          )
        ) : (
          <Text type="secondary" className="mb-2 block text-xs">
            暂无示例图
          </Text>
        )}
        <Space wrap>
          <Upload
            accept={IMAGE_ACCEPT}
            showUploadList={false}
            beforeUpload={beforeUpload}
            customRequest={({ file, onSuccess, onError }) => {
              setUploading(true)
              save.mutate(
                { run: () => api.uploadGuideImage(group.id, file as File), okMsg: '示例图已上传' },
                {
                  onSuccess: () => onSuccess?.(null),
                  onError: (e) => onError?.(e as Error),
                  onSettled: () => setUploading(false),
                },
              )
            }}
          >
            <Button icon={<UploadCloud size={14} />} loading={uploading} disabled={save.isPending && !uploading}>
              {current ? '上传替换' : '上传图片'}
            </Button>
          </Upload>
          {current && (
            <Popconfirm
              title="清除示例图？"
              description="guide_image 字段将置空（已上传的文件本身不删除）"
              okText="清除"
              okButtonProps={{ danger: true }}
              cancelText="取消"
              onConfirm={() =>
                save.mutate({
                  // PUT 为字段部分更新：显式传 null 即清空该字段，其余字段不受影响
                  run: () => api.updateSourceGroup(group.id, { guide_image: null }),
                  okMsg: '示例图已清除',
                })
              }
            >
              <Button danger icon={<Trash2 size={14} />} disabled={save.isPending || uploading}>
                清除
              </Button>
            </Popconfirm>
          )}
        </Space>
        <div className="mt-3 max-w-md">
          <div className="mb-1 text-xs text-muted">或直接填写图片地址（根相对路径 / 外部 http URL）</div>
          <Space.Compact className="w-full">
            <Input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="如：/api/static/guide-images/cmb.png 或 https://…"
              allowClear
            />
            <Button
              type="primary"
              loading={save.isPending && !uploading}
              disabled={!dirty || uploading}
              onClick={() =>
                save.mutate({
                  run: () => api.updateSourceGroup(group.id, { guide_image: url.trim() || null }),
                  okMsg: url.trim() ? '示例图地址已保存' : '示例图地址已清空',
                })
              }
            >
              保存
            </Button>
          </Space.Compact>
        </div>
        <div className="mt-1 max-w-md text-xs text-muted">
          上传支持 png / jpg / jpeg / webp ≤ 5MB，存入后端静态目录（/api/static/guide-images/…）；点击预览图可放大查看
        </div>
      </div>
    </div>
  )
}

function RulesDrawerBody({ group }: { group: SourceGroup }) {
  const [tab, setTab] = useState<RulesTab>('correction')
  // 三个面板保持挂载（hidden 切换），跨 Tab 不丢未保存的表单状态
  return (
    <div className="space-y-4">
      <Segmented
        block
        value={tab}
        onChange={(v) => setTab(v as RulesTab)}
        options={[
          { label: '校正规则', value: 'correction' },
          { label: '解析规则', value: 'parse' },
          { label: '指引与示例图', value: 'guide' },
        ]}
      />
      <div className={tab === 'correction' ? '' : 'hidden'}>
        <CorrectionRulesPanel group={group} />
      </div>
      <div className={tab === 'parse' ? '' : 'hidden'}>
        <ParseRulesPanel group={group} />
      </div>
      <div className={tab === 'guide' ? '' : 'hidden'}>
        <GuidePanel group={group} />
      </div>
    </div>
  )
}

interface GroupFormValues {
  slug?: string
  label: string
  asset_prefix?: string
  guide?: string
  account_ids?: number[]
  enabled?: boolean
  sort_order?: number
}

// useSearchParams 在静态预渲染时需 Suspense 包裹（App Router CSR bailout），页面逻辑放在内层组件
export default function SourceGroupsPage() {
  return (
    <Suspense fallback={null}>
      <SourceGroupsPageContent />
    </Suspense>
  )
}

function SourceGroupsPageContent() {
  const queryClient = useQueryClient()
  const router = useRouter()
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<SourceGroup | null>(null)
  // 规则管理 Drawer：只存组 id，展示数据从 live query 按 id 取（组被删时自动关闭）
  const [rulesGroupId, setRulesGroupId] = useState<number | null>(null)
  /** 待删的来源组（弹出理由框）；null = 框关着。 */
  const [deletingGroup, setDeletingGroup] = useState<SourceGroup | null>(null)
  /** 删除框内的提示（缺理由时出现；弹窗不关）。 */
  const [deleteGroupError, setDeleteGroupError] = useState<string | null>(null)
  const [form] = Form.useForm<GroupFormValues>()

  // 理由草稿按来源组保留：关窗不清、换一组才清
  const [deleteGroupReason, setDeleteGroupReason] = useAuditReasonDraft(deletingGroup ? deletingGroup.id : null)
  // 编辑态 slug 实时监听：改动时在字段下方给迁移警示，未改动不显示
  const watchedSlug = Form.useWatch('slug', form)
  const slugChanged =
    !!editing && (watchedSlug ?? '').trim() !== '' && (watchedSlug ?? '').trim() !== editing.slug

  // 与截图录入页共用 queryKey：这里 CRUD 后，截图页的渠道下拉一并刷新
  const { data: groups = [], isLoading } = useQuery({
    queryKey: ['source-groups'],
    queryFn: api.getSourceGroups,
  })
  // 关联账户多选项：账户维度已并入机构（0027 起 source_groups.account_ids 指向 institutions）
  const { data: institutionsData } = useQuery({
    queryKey: ['institutions'],
    queryFn: api.getInstitutions,
  })
  const accounts = useMemo(() => institutionsData?.items ?? [], [institutionsData])

  const accountName = useMemo(() => {
    const map = new Map<number, string>()
    for (const a of accounts) map.set(a.id, a.name)
    return map
  }, [accounts])

  // Drawer 展示的组：live 数据（规则写操作后即时反映），不用打开时快照
  const drawerGroup = useMemo(
    () => (rulesGroupId != null ? groups.find((g) => g.id === rulesGroupId) ?? null : null),
    [rulesGroupId, groups],
  )

  // 仪表盘补录弹窗「去关联」直达：?bind_account=<id>（非法或账户不存在时静默忽略）
  const searchParams = useSearchParams()
  const bindAccountId = useMemo(() => {
    const raw = searchParams.get('bind_account')
    const id = raw ? Number(raw) : NaN
    return Number.isInteger(id) && id > 0 ? id : undefined
  }, [searchParams])
  const bindAccount = bindAccountId ? accounts.find((a) => a.id === bindAccountId) : undefined
  // 清除引导：抹掉 query 回到普通管理态（replace 不留历史记录）
  const clearBind = () => router.replace('/settings/source-groups')

  // 关联动作按组 id 独立跟踪 pending，允许连续关联多个渠道
  const [bindingIds, setBindingIds] = useState<Set<number>>(() => new Set())
  const bindMutation = useMutation({
    mutationFn: ({ groupId, accountIds }: { groupId: number; label: string; name: string; accountIds: number[] }) =>
      api.updateSourceGroup(groupId, { account_ids: accountIds }),
    onMutate: ({ groupId }) => {
      setBindingIds((prev) => new Set(prev).add(groupId))
    },
    onSuccess: (_res, { label, name }) => {
      message.success(`已将「${name}」关联到「${label}」`)
      queryClient.invalidateQueries({ queryKey: ['source-groups'] })
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
    },
    onError: (e) => message.error(e.message),
    onSettled: (_res, _err, { groupId }) => {
      setBindingIds((prev) => {
        const next = new Set(prev)
        next.delete(groupId)
        return next
      })
    },
  })

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['source-groups'] })

  // Modal 顶部规则摘要：基于 live 数据（Drawer 里改动后摘要即时同步），
  // editing 快照仅作 refetch 间隙的回落
  const liveEditing = editing ? groups.find((g) => g.id === editing.id) ?? editing : null
  const editingSummary = useMemo(() => {
    if (!liveEditing) return null
    const raw = rawRulesOf(liveEditing.rules)
    if (!raw) return { broken: true as const }
    const parsed = parseGroupRules(liveEditing.rules)
    const rename = parsed?.rename.length ?? 0
    const ignore = parsed?.ignore.length ?? 0
    const exclude = parsed?.exclude.length ?? 0
    return {
      broken: false as const,
      rename,
      ignore,
      exclude,
      correction: rename + ignore + exclude,
      parseKeys: PARSE_KEYS.filter((k) => k in raw).length,
      hasImage: !!liveEditing.guide_image,
    }
  }, [liveEditing])

  const createMutation = useMutation({
    mutationFn: (data: Partial<SourceGroup>) => api.createSourceGroup(data),
    onSuccess: () => {
      invalidate()
      close()
      message.success('来源组已创建')
    },
    onError: (e) => message.error(e.message),
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: Partial<SourceGroup> }) =>
      api.updateSourceGroup(id, data),
    onSuccess: () => {
      invalidate()
      close()
      message.success('来源组已更新')
    },
    onError: (e) => message.error(e.message),
  })

  // 启用开关独立 mutation：行内切换不与弹窗保存共享 confirmLoading
  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: number; label: string; enabled: boolean }) =>
      api.updateSourceGroup(id, { enabled }),
    onSuccess: (_res, { label, enabled }) => {
      invalidate()
      message.success(enabled ? `已启用「${label}」` : `已停用「${label}」`)
    },
    onError: (e) => message.error(e.message),
  })

  const deleteMutation = useMutation({
    mutationFn: ({ id, audit }: { id: number; audit?: AuditReasonInput }) => api.deleteSourceGroup(id, audit),
    onSuccess: () => {
      invalidate()
      message.success('来源组已删除')
      setDeletingGroup(null)
      setDeleteGroupError(null)
    },
    onError: (e: Error) => {
      // 缺理由（422）不是「删不掉」：弹窗与已选理由都留着，补一句再点一次。
      if (isAuditReasonRequired(e)) {
        setDeleteGroupError(AUDIT_REASON_REQUIRED_TEXT)
        return
      }
      // 其余错误原样展示后端 400 的中文 detail（仍关联账户/被凭证引用）
      message.error(e.message)
    },
  })

  const openAdd = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({ enabled: true, sort_order: 0, account_ids: [] })
    setModalOpen(true)
  }

  const openEdit = (record: SourceGroup) => {
    setEditing(record)
    form.resetFields()
    form.setFieldsValue({
      slug: record.slug,
      label: record.label,
      asset_prefix: record.asset_prefix ?? '',
      guide: record.guide ?? undefined,
      account_ids: record.account_ids ?? [],
      enabled: record.enabled,
      sort_order: record.sort_order ?? 0,
    })
    setModalOpen(true)
  }

  const close = () => {
    setModalOpen(false)
    setEditing(null)
    form.resetFields()
  }

  const handleSubmit = async () => {
    const values = await form.validateFields()
    // 不携带 rules / guide_image：PUT 按字段部分更新，不携带即不改动；
    // 规则与示例图在「规则管理」Drawer 中独立维护（独立写入路径）
    const payload: Partial<SourceGroup> = {
      label: values.label.trim(),
      asset_prefix: values.asset_prefix?.trim() ?? '',
      guide: values.guide ?? null,
      account_ids: values.account_ids ?? [],
      enabled: values.enabled ?? true,
      sort_order: values.sort_order ?? 0,
    }
    if (editing) {
      // slug 可改：凭证存的是 source_group_id 外键（迁移 0029），改名天然不影响历史凭证，
      // 后端不再需要任何级联同步；唯一冲突走 400 中文 detail，由 updateMutation 原样展示
      updateMutation.mutate({ id: editing.id, data: { ...payload, slug: values.slug!.trim() } })
    } else {
      // slug 留空时后端自动生成 sg-xxxxxxxx
      createMutation.mutate({ ...payload, slug: values.slug?.trim() || undefined })
    }
  }

  const columns: ColumnsType<SourceGroup> = [
    {
      title: '标签',
      dataIndex: 'label',
      width: 160,
      key: 'label',
      render: (label: string, record) => {
        // 规则数徽标：N = 校正规则条数 + 解析键数；坏 JSON 显示「规则异常」；0 隐藏
        const raw = rawRulesOf(record.rules)
        const parsed = parseGroupRules(record.rules)
        const n = parsed
          ? parsed.rename.length +
            parsed.ignore.length +
            parsed.exclude.length +
            (raw ? PARSE_KEYS.filter((k) => k in raw).length : 0)
          : 0
        return (
          <Space size={4} wrap>
            <Text strong>{label}</Text>
            {!record.enabled && <Tag className="!mr-0">已停用</Tag>}
            {raw === null ? (
              <Tooltip title="rules 字段无法解析为 JSON，点击查看原始内容">
                <button
                  type="button"
                  onClick={() => setRulesGroupId(record.id)}
                  className="relative inline-flex items-center rounded-pill border border-status-warn/40 bg-status-warn-surface px-2 py-0.5 text-meta text-status-warn-text after:absolute after:inset-x-0 after:-inset-y-2.5 after:content-['']"
                  aria-label={`查看${record.label}的规则异常`}
                >
                  规则异常
                </button>
              </Tooltip>
            ) : n > 0 ? (
              <Tooltip title="校正规则 + 解析规则，点击打开规则管理">
                <button
                  type="button"
                  onClick={() => setRulesGroupId(record.id)}
                  className="relative inline-flex items-center rounded-pill border border-brand-200 bg-brand-50 px-2 py-0.5 text-meta text-brand-700 after:absolute after:inset-x-0 after:-inset-y-2.5 after:content-['']"
                  aria-label={`管理${record.label}的${n}条规则`}
                >
                  规则 {n}
                </button>
              </Tooltip>
            ) : null}
          </Space>
        )
      },
    },
    {
      title: 'Slug',
      dataIndex: 'slug',
      width: 120,
      key: 'slug',
      render: (slug: string) => <Text code>{slug}</Text>,
    },
    {
      title: '资产名前缀',
      dataIndex: 'asset_prefix',
      key: 'asset_prefix',
      render: (v: string | null) => (v ? <Text code>{v}</Text> : <Text type="secondary">--</Text>),
    },
    {
      title: '启用',
      dataIndex: 'enabled',
      key: 'enabled',
      width: 80,
      render: (enabled: boolean, record) => (
        <Switch
          size="small"
          checked={enabled}
          loading={toggleMutation.isPending && toggleMutation.variables?.id === record.id}
          onChange={(v) => toggleMutation.mutate({ id: record.id, label: record.label, enabled: v })}
        />
      ),
    },
    {
      title: '排序',
      dataIndex: 'sort_order',
      key: 'sort_order',
      width: 70,
      align: 'center',
      responsive: ['lg'],
    },
    {
      title: '关联账户',
      dataIndex: 'account_ids',
      key: 'account_ids',
      responsive: ['lg'],
      render: (ids: number[]) => {
        if (!ids?.length) return <Text type="secondary">--</Text>
        const nameOf = (id: number) => accountName.get(id) ?? `#${id}`
        const shown = ids.slice(0, 3)
        const rest = ids.length - shown.length
        return (
          <Space size={4} wrap>
            {shown.map((id) => (
              <Tag key={id} className="!mr-0">{nameOf(id)}</Tag>
            ))}
            {rest > 0 && (
              <Tooltip title={ids.slice(3).map(nameOf).join('、')}>
                <Tag className="!mr-0">+{rest}</Tag>
              </Tooltip>
            )}
          </Space>
        )
      },
    },
    // 绑定引导激活时插入快捷关联列（PUT account_ids 提供即整体替换：回传原列表 + 新账户）
    ...(bindAccount
      ? ([
          {
            title: `关联「${bindAccount.name}」`,
            key: 'bind',
            width: 160,
            render: (_: unknown, record: SourceGroup) => {
              const linked = record.account_ids?.includes(bindAccount.id)
              return (
                <Space size={4} wrap>
                  <Button
                    size="small"
                    type={linked ? undefined : 'primary'}
                    disabled={linked}
                    loading={bindingIds.has(record.id)}
                    onClick={() =>
                      bindMutation.mutate({
                        groupId: record.id,
                        label: record.label,
                        name: bindAccount.name,
                        accountIds: [...(record.account_ids ?? []), bindAccount.id],
                      })
                    }
                  >
                    {linked ? '已关联' : '关联此账户'}
                  </Button>
                  {!record.enabled && (
                    <Text type="secondary" className="!text-xs">渠道已停用</Text>
                  )}
                </Space>
              )
            },
          },
        ] as ColumnsType<SourceGroup>)
      : []),
    {
      title: '操作',
      key: 'action',
      width: 140,
      render: (_: unknown, record) => (
        <Space size="small">
          <Button size="small" onClick={() => openEdit(record)}>
            编辑
          </Button>
          <Button size="small" danger className="!text-status-critical-text" onClick={() => setDeletingGroup(record)}>
            删除
          </Button>
        </Space>
      ),
    },
  ]

  return (
    <div className="space-y-6">
      <PageHeader
        title="来源组"
        description="截图来源渠道、账户绑定与规则说明。"
        actions={<Button type="primary" icon={<Plus size={16} />} onClick={openAdd}>新增来源组</Button>}
      />

      {bindAccount ? (
        <Alert
          type="warning"
          showIcon
          closable
          onClose={clearBind}
          title={`正在为「${bindAccount.name}」关联截图来源`}
          description="点击对应渠道行的「关联此账户」，可连续关联多个渠道；完成后点右侧 × 退出"
        />
      ) : (
        <Alert
          type="info"
          showIcon
          title="截图录入渠道：一个 App 一组。上传截图时按文件名识别 App 自动匹配来源组，写入的资产名按「名前缀」模板生成，并映射到关联账户。"
        />
      )}

      <Card>
        <div className="hidden md:block">
          <Table
            rowKey="id"
            columns={columns}
            dataSource={groups}
            loading={isLoading}
            pagination={false}
            scroll={{ x: 680 }}
            locale={{ emptyText: '暂无来源组，点击右上角「新增来源组」添加' }}
          />
        </div>
        <div className="divide-y divide-line md:hidden">
          {isLoading ? (
            <p className="py-8 text-center text-body text-muted">加载中…</p>
          ) : groups.length === 0 ? (
            <p className="py-8 text-center text-body text-muted">暂无来源组，点击右上角「新增来源组」添加</p>
          ) : groups.map((record) => {
            const raw = rawRulesOf(record.rules)
            const parsed = parseGroupRules(record.rules)
            const ruleCount = parsed
              ? parsed.rename.length + parsed.ignore.length + parsed.exclude.length +
                (raw ? PARSE_KEYS.filter((k) => k in raw).length : 0)
              : 0
            const accountNames = (record.account_ids ?? []).map((id) => accountName.get(id) ?? `#${id}`)
            const linked = !!bindAccount && record.account_ids?.includes(bindAccount.id)
            return (
              <div key={record.id} className="space-y-3 py-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <p className="text-body-strong font-medium text-ink">{record.label}</p>
                    {!record.enabled && <span className="rounded-pill bg-subtle px-2 py-0.5 text-meta text-muted">已停用</span>}
                    <button
                      type="button"
                      onClick={() => setRulesGroupId(record.id)}
                      className="inline-flex min-h-[44px] items-center rounded-pill border border-line-strong bg-surface px-2 text-meta text-secondary"
                    >
                      {raw === null ? '规则异常' : ruleCount > 0 ? `规则 ${ruleCount}` : '规则'}
                    </button>
                  </div>
                  <Switch
                    checked={record.enabled}
                    loading={toggleMutation.isPending && toggleMutation.variables?.id === record.id}
                    onChange={(v) => toggleMutation.mutate({ id: record.id, label: record.label, enabled: v })}
                    aria-label={record.enabled ? `停用${record.label}` : `启用${record.label}`}
                  />
                </div>
                <dl className="grid grid-cols-2 gap-x-3 gap-y-2 text-meta">
                  <div><dt className="text-muted">Slug</dt><dd className="break-all text-secondary">{record.slug}</dd></div>
                  <div><dt className="text-muted">资产名前缀</dt><dd className="break-all text-secondary">{record.asset_prefix || '—'}</dd></div>
                  <div><dt className="text-muted">排序</dt><dd className="pf-num text-secondary">{record.sort_order}</dd></div>
                  <div><dt className="text-muted">关联账户</dt><dd className="text-secondary">{accountNames.length ? accountNames.join('、') : '—'}</dd></div>
                </dl>
                {bindAccount && (
                  <button
                    type="button"
                    disabled={linked || bindingIds.has(record.id)}
                    onClick={() => bindMutation.mutate({
                      groupId: record.id,
                      label: record.label,
                      name: bindAccount.name,
                      accountIds: [...(record.account_ids ?? []), bindAccount.id],
                    })}
                    className="inline-flex min-h-[44px] w-full items-center justify-center rounded-control border border-line-strong bg-surface px-3 text-meta font-medium text-ink disabled:opacity-50"
                  >
                    {linked ? '已关联此账户' : `关联「${bindAccount.name}」`}
                  </button>
                )}
                <div className="grid grid-cols-2 gap-2">
                  <button type="button" onClick={() => setRulesGroupId(record.id)} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-2 text-meta font-medium text-ink">规则管理</button>
                  <button type="button" onClick={() => openEdit(record)} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-2 text-meta font-medium text-ink">编辑</button>
                  <button type="button" onClick={() => setDeletingGroup(record)} className="col-span-2 inline-flex min-h-[44px] items-center justify-center rounded-control border border-status-critical/30 bg-surface px-2 text-meta font-medium text-status-critical-text">删除</button>
                </div>
              </div>
            )
          })}
        </div>
      </Card>

      <Modal
        title={editing ? '编辑来源组' : '新增来源组'}
        open={modalOpen}
        onOk={handleSubmit}
        onCancel={close}
        okText={editing ? '保存修改' : '创建'}
        cancelText="取消"
        confirmLoading={createMutation.isPending || updateMutation.isPending}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" className="mt-4">
          {editing && editingSummary && (
            <div className="mb-4 flex flex-wrap items-center justify-between gap-y-2 gap-3 rounded-lg border border-slate-200 bg-slate-50/60 px-3 py-2">
              <span className="min-w-0 text-xs leading-5 text-slate-500">
                {editingSummary.broken ? (
                  <span className="text-amber-600">
                    rules JSON 无法解析（此处保存不会改动它），可打开「管理规则」查看原始内容
                  </span>
                ) : editingSummary.correction + editingSummary.parseKeys === 0 && !editingSummary.hasImage ? (
                  <>尚未配置校正/解析规则，可通过「管理规则」添加</>
                ) : (
                  <>
                    校正规则 {editingSummary.correction} 条（改名 {editingSummary.rename} · 忽略{' '}
                    {editingSummary.ignore} · 不录入 {editingSummary.exclude}）
                    {editingSummary.parseKeys > 0 && <> · 解析规则已配置</>}
                    {editingSummary.hasImage && <> · 示例图地址已配置</>}
                    <span className="text-muted">；此处保存不会改动它们</span>
                  </>
                )}
              </span>
              <Button size="small" className="shrink-0" onClick={() => setRulesGroupId(editing.id)}>
                管理规则
              </Button>
            </div>
          )}
          <Form.Item
            name="label"
            label="标签"
            rules={[{ required: true, whitespace: true, message: '请输入来源组标签' }]}
            extra="截图录入页展示的渠道名，需唯一"
          >
            <Input placeholder="如：招商银行" autoFocus />
          </Form.Item>
          <Form.Item
            name="slug"
            label="Slug"
            rules={
              editing
                ? [
                    { required: true, whitespace: true, message: '请输入 Slug' },
                    { pattern: SLUG_PATTERN, message: SLUG_PATTERN_MSG },
                  ]
                : [{ pattern: SLUG_PATTERN, message: SLUG_PATTERN_MSG }]
            }
            extra={
              editing ? (
                slugChanged ? (
                  <span className="text-amber-600">
                    保存后「{editing.slug}」名下的历史截图凭证将改挂到新标识，资产数据不受影响
                  </span>
                ) : (
                  '标识用于系统内部引用与截图渠道匹配；已有历史录入的组慎改'
                )
              ) : (
                '留空自动生成，如 cmb'
              )
            }
          >
            <Input placeholder="如：cmb" />
          </Form.Item>
          <Form.Item
            name="asset_prefix"
            label="资产名前缀"
            extra="截图写入资产名的前缀模板，如：支付宝-"
          >
            <Input placeholder="如：招行-" />
          </Form.Item>
          <Form.Item
            name="account_ids"
            label="关联账户"
            extra="该渠道截图覆盖哪些账户；删除来源组前需先清空关联"
          >
            <Select
              mode="multiple"
              placeholder="选择账户"
              optionFilterProp="label"
              options={accounts.map((a) => ({ value: a.id, label: a.name }))}
            />
          </Form.Item>
          <Form.Item name="guide" label="录入指引" extra="截图录入页展示，提醒怎么截、截哪个页面">
            <Input.TextArea rows={3} placeholder="如：打开招行 App → 我的 → 截取资产总览页" />
          </Form.Item>
          <div className="flex gap-8">
            <Form.Item name="enabled" label="启用" valuePropName="checked">
              <Switch />
            </Form.Item>
            <Form.Item name="sort_order" label="排序" tooltip="数值小的排在前面">
              <InputNumber min={0} precision={0} />
            </Form.Item>
          </div>
        </Form>
      </Modal>

      <Drawer
        title={drawerGroup ? `规则管理 · ${drawerGroup.label}` : '规则管理'}
        size={640}
        open={!!drawerGroup}
        onClose={() => setRulesGroupId(null)}
        destroyOnHidden
        /* 层号走 antd 自己的容器带（zIndexPopupBase=1000 起），不与应用自绘弹层的
           z-sticky/nav/overlay/modal(10–50) 混用：自绘弹层只负责盖页面内容，
           antd 组件之间由 antd 自己叠放。取 1100 = 1000 + 一层容器偏移，同时满足：
           - 压住「编辑来源组」弹窗（antd Modal 1000）——弹窗里的「管理规则」正是走这条；
           - 低于本抽屉内的确认 Modal（antd 自动叠到 1200）与 toast（message 2010）。 */
        zIndex={1100}
      >
        {drawerGroup && <RulesDrawerBody group={drawerGroup} />}
      </Drawer>

      {/* 删除来源组：理由必填（缺 reason 后端 422）。删组会连带删掉该组名下的历史凭证，
          所以后果写全；缺理由时确认按钮不发请求，422 也留在框里补一句。 */}
      <ConfirmDialog
        open={!!deletingGroup}
        onClose={() => {
          setDeletingGroup(null)
          setDeleteGroupError(null)
        }}
        onConfirm={() => deletingGroup && deleteMutation.mutate({
          id: deletingGroup.id,
          audit: toAuditReasonInput(deleteGroupReason),
        })}
        title={deletingGroup ? `删除来源组「${deletingGroup.label}」？` : '删除来源组？'}
        description="仍关联账户或已被截图凭证引用时无法删除；删除后不可恢复。"
        confirmLabel="删除来源组"
        danger
        loading={deleteMutation.isPending}
      >
        <div className="space-y-3">
          {deleteGroupError && <InlineError>{deleteGroupError}</InlineError>}
          <AuditReasonField
            label="为什么删"
            value={deleteGroupReason}
            onChange={setDeleteGroupReason}
            hint="删除会留下谁在何时删了哪一个来源组。"
          />
        </div>
      </ConfirmDialog>
    </div>
  )
}
