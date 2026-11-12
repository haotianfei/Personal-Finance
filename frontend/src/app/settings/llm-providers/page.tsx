'use client'

import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { Plus } from 'lucide-react'
import { api } from '@/lib/api'
import { AUDIT_REASON_REQUIRED_TEXT, isAuditReasonRequired } from '@/lib/api'
import type { AuditReasonInput } from '@/lib/api'
import type {
  LlmProvider,
  LlmProviderCreate,
  LlmProviderKind,
  LlmProviderUpdate,
} from '@/types'
import { PageHeader } from '@/components/layout/PageHeader'
import { ConfirmDialog, InlineError } from '@/components/ui'
import {
  AuditReasonField,
  toAuditReasonInput,
  useAuditReasonDraft,
} from '@/components/records/AuditReasonField'
import { useMediaQuery } from '@/hooks/useMediaQuery'
import { formatRelativeTime } from '@/lib/format'

const { Text } = Typography

const KIND_OPTIONS: { value: LlmProviderKind; label: string }[] = [
  { value: 'deepseek', label: 'DeepSeek' },
  { value: 'dashscope', label: 'DashScope（阿里云百炼）' },
  { value: 'openai_compatible', label: '自定义 OpenAI 兼容' },
]

const kindLabel = (kind: LlmProviderKind) =>
  KIND_OPTIONS.find((o) => o.value === kind)?.label ?? kind

const kindShortLabel = (kind: LlmProviderKind) =>
  kind === 'dashscope' ? 'DashScope' : kind === 'openai_compatible' ? 'OpenAI 兼容' : kindLabel(kind)

/** 测试状态徽章的三个分支（ADR-0025 §1.2：未测试 / 成功 / 失败）。 */
type TestStatus = 'untested' | 'ok' | 'fail'

/**
 * 三态判定：**先看 `last_tested_at`，再看 `last_test_ok`**。
 *
 * 后端契约里 `NULL` ＝ 从未测试、`false` ＝ 测过且失败，而 `null` 与 `false` 都是
 * falsy——若按「`!== true` 就算失败」判，新加的供应商一进门就被标成「失败」。
 * 时间戳有值但结论缺失（正常不会出现）→ 归「未测试」，不臆造结论。
 */
const testStatusOf = (record: LlmProvider): TestStatus => {
  if (record.last_tested_at == null) return 'untested'
  if (record.last_test_ok === true) return 'ok'
  if (record.last_test_ok === false) return 'fail'
  return 'untested'
}

/**
 * 徽章配色一律取既有的 status.* / subtle 语义 token，不新增色板。
 * 成功态用 `status-good` 的 10% 透明度底而非 `status-good-surface`：后者不在
 * `tokens.json` 里（仓库多处引用但类未生成），用了会变成没有底色的裸文字。
 */
const TEST_STATUS_CLASS: Record<TestStatus, string> = {
  untested: 'bg-subtle text-muted',
  ok: 'bg-status-good/10 text-status-good-text',
  fail: 'bg-status-critical-surface text-status-critical-text',
}

/** 失败摘要截断长度（字符）：徽章区宽度有限，超长截断加省略号，不换行。 */
const FAILURE_MESSAGE_MAX = 40

const truncateMessage = (text: string): string =>
  text.length > FAILURE_MESSAGE_MAX ? `${text.slice(0, FAILURE_MESSAGE_MAX)}…` : text

/**
 * 测试状态徽章（成功态带相对时间与耗时，失败态带相对时间与摘要）。
 *
 * 桌面 Table 与移动卡片**共用**这一个组件，避免两处各判一遍而漂移。
 * 徽章内不加 tooltip（`title=` / antd `Tooltip`），与既有「凭证」徽章惯例一致。
 */
function TestStatusBadge({ record }: { record: LlmProvider }) {
  const status = testStatusOf(record)
  // 未测试的行不显示相对时间——「从未发生」不该渲染成「刚刚」
  const relative = formatRelativeTime(record.last_tested_at)
  const elapsed = record.last_test_elapsed_ms
  const label =
    status === 'untested'
      ? '未测试'
      : status === 'ok'
        ? `已通过 · ${relative}${typeof elapsed === 'number' ? ` · ${elapsed}ms` : ''}`
        : `失败 · ${relative}`
  const message = record.last_test_message?.trim()

  return (
    <span className="flex min-w-0 flex-col items-start gap-1">
      <span className={`whitespace-nowrap rounded-pill px-2 py-0.5 text-meta ${TEST_STATUS_CLASS[status]}`}>
        {label}
      </span>
      {status === 'fail' && message && (
        <span className="max-w-[220px] truncate text-micro text-muted">{truncateMessage(message)}</span>
      )}
    </span>
  )
}

interface ProviderFormValues {
  label: string
  kind: LlmProviderKind
  base_url: string
  model: string
  api_key?: string
  is_default?: boolean
  enabled?: boolean
}

export default function LlmProvidersPage() {
  const queryClient = useQueryClient()
  const [modalOpen, setModalOpen] = useState(false)
  const isWide = useMediaQuery('(min-width: 1280px)', true)
  const [editing, setEditing] = useState<LlmProvider | null>(null)
  const [deleting, setDeleting] = useState<LlmProvider | null>(null)
  /** 删除框内的提示（缺理由时出现；弹窗不关）。 */
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [form] = Form.useForm<ProviderFormValues>()

  // 理由草稿按供应商保留：关窗不清、换一家才清
  const [deleteReason, setDeleteReason] = useAuditReasonDraft(deleting ? deleting.id : null)

  const { data: providers = [], isLoading } = useQuery({
    queryKey: ['llm-providers'],
    queryFn: api.getLlmProviders,
  })

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['llm-providers'] })

  const createMutation = useMutation({
    mutationFn: (data: LlmProviderCreate) => api.createLlmProvider(data),
    onSuccess: () => {
      invalidate()
      close()
      message.success('供应商已创建')
    },
    onError: (e) => message.error(e.message),
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: LlmProviderUpdate }) =>
      api.updateLlmProvider(id, data),
    onSuccess: () => {
      invalidate()
      close()
      message.success('供应商已更新')
    },
    onError: (e) => message.error(e.message),
  })

  const deleteMutation = useMutation({
    mutationFn: ({ id, audit }: { id: number; audit?: AuditReasonInput }) => api.deleteLlmProvider(id, audit),
    onSuccess: () => {
      invalidate()
      message.success('供应商已删除')
      setDeleting(null)
      setDeleteError(null)
    },
    onError: (e: Error) => {
      // 缺理由（422）不是删除失败：把弹窗和已选的理由都留着，补一句再点一次。
      if (isAuditReasonRequired(e)) {
        setDeleteError(AUDIT_REASON_REQUIRED_TEXT)
        return
      }
      message.error(e.message)
    },
  })

  // 连通性测试：结果按行区分 loading（variables 携带 label 用于提示文案）
  const testMutation = useMutation({
    mutationFn: ({ id }: { id: number; label: string }) => api.testLlmProvider(id),
    onSuccess: (res, { label }) => {
      // 测试结果由后端写库（ADR-0025），toast 之外徽章也要立刻跟上：
      // 失效列表查询后按新结果重渲染，不在这里手改本地数据，避免两份真相。
      invalidate()
      if (res.ok) {
        message.success(`${label}：连接成功（${res.elapsed_ms}ms）${res.message ? ' — ' + res.message : ''}`)
      } else {
        message.error(`${label}：${res.message}`)
      }
    },
    // 空 key 等场景后端返回 400，经 request 抛 Error，这里只弹一次错误提示
    onError: (e, { label }) => message.error(`${label}：${e.message}`),
  })

  const toggleEnabled = (record: LlmProvider, enabled: boolean) => {
    updateMutation.mutate({ id: record.id, data: { enabled } })
  }

  const markDefault = (record: LlmProvider) => {
    updateMutation.mutate({ id: record.id, data: { is_default: true } })
  }

  /**
   * 取消默认：只把 `is_default` 置假，**不**指定接任者——允许全表零默认（spec D4）。
   * 删除默认行时后端会自动提拔下一家（spec D5），那是后端行为，前端不复制这套逻辑。
   * 走同一个 `updateMutation`，成功后由它的 `onSuccess` 失效列表查询。
   */
  const unmarkDefault = (record: LlmProvider) => {
    updateMutation.mutate({ id: record.id, data: { is_default: false } })
  }

  const openAdd = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({ kind: 'deepseek', enabled: true, is_default: false })
    setModalOpen(true)
  }

  const openEdit = (record: LlmProvider) => {
    setEditing(record)
    form.resetFields()
    form.setFieldsValue({
      label: record.label,
      kind: record.kind,
      base_url: record.base_url,
      model: record.model,
      api_key: undefined,
      is_default: record.is_default,
      enabled: record.enabled,
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
    if (editing) {
      const data: LlmProviderUpdate = {
        label: values.label,
        kind: values.kind,
        base_url: values.base_url,
        model: values.model,
        is_default: values.is_default,
        enabled: values.enabled,
      }
      // api_key 留空 = 不修改 key
      if (values.api_key?.trim()) data.api_key = values.api_key.trim()
      updateMutation.mutate({ id: editing.id, data })
    } else {
      createMutation.mutate(values as LlmProviderCreate)
    }
  }

  const columns: ColumnsType<LlmProvider> = [
    {
      title: '名称',
      dataIndex: 'label',
      key: 'label',
      width: 160,
      render: (label: string, record) => (
        <div>
          <Space>
            <Text strong>{label}</Text>
            {record.is_default && <Tag color="gold">默认</Tag>}
          </Space>
          <p className="mt-0.5 text-meta text-muted xl:hidden">{kindShortLabel(record.kind)} · {record.model}</p>
        </div>
      ),
    },
    {
      title: '类型',
      dataIndex: 'kind',
      key: 'kind',
      width: 110,
      responsive: ['xl'],
      render: (kind: LlmProviderKind) => <Tag>{kindShortLabel(kind)}</Tag>,
    },
    {
      title: 'Base URL',
      dataIndex: 'base_url',
      key: 'base_url',
      width: 220,
      responsive: ['xl'],
      ellipsis: true,
      render: (v: string) => <Text copyable={{ text: v }}>{v}</Text>,
    },
    { title: '模型', dataIndex: 'model', key: 'model', width: 150, responsive: ['xl'] },
    {
      title: 'API Key',
      dataIndex: 'api_key_masked',
      key: 'api_key_masked',
      width: 170,
      responsive: ['xl'],
      render: (v: string | null) => <Text type="secondary">{v || '--'}</Text>,
    },
    {
      title: '启用',
      dataIndex: 'enabled',
      key: 'enabled',
      width: 80,
      render: (enabled: boolean, record) => (
        <Switch checked={enabled} onChange={(v) => toggleEnabled(record, v)} />
      ),
    },
    {
      title: '操作',
      key: 'action',
      width: 260,
      render: (_: unknown, record) => {
        const testing = testMutation.isPending && testMutation.variables?.id === record.id
        return (
          <div className="space-y-1.5">
            {/* 测试状态徽章（票 02）：桌面与移动卡片共用同一组件 */}
            <TestStatusBadge record={record} />
            <Space size="small">
              <Button
                size="small"
                loading={testing}
                disabled={testMutation.isPending && !testing}
                onClick={() => testMutation.mutate({ id: record.id, label: record.label })}
              >
                {testing ? '测试中…' : '测试'}
              </Button>
              <Button size="small" onClick={() => openEdit(record)}>
                编辑
              </Button>
              {/* 默认行给反向入口「取消默认」，与「设为默认」**同一位置互补**（spec D4）：
                  同一时刻只渲染其一，不并排。二次确认必须点明零默认的两点后果。 */}
              {record.is_default ? (
                <Popconfirm
                  title="取消默认供应商？"
                  description="取消后不会自动使用任何供应商：截图一致性校验会标为「未校验」，投资建议会提示先设置默认供应商。"
                  okText="取消默认"
                  cancelText="保留默认"
                  onConfirm={() => unmarkDefault(record)}
                >
                  <Button size="small">
                    取消默认
                  </Button>
                </Popconfirm>
              ) : (
                <Button size="small" onClick={() => markDefault(record)}>
                  设为默认
                </Button>
              )}
              <Button
                size="small"
                danger
                className="!text-status-critical-text"
                onClick={() => setDeleting(record)}
              >
                删除
              </Button>
            </Space>
          </div>
        )
      },
    },
  ]

  return (
    <div className="space-y-6">
      <PageHeader
        title="LLM 供应商"
        description="截图一致性校验使用的模型供应商；Key 仅存本地数据库。"
        actions={<Button type="primary" icon={<Plus size={16} />} onClick={openAdd}>新增供应商</Button>}
      />

      <Alert
        type="info"
        showIcon
        title="AI 用于截图一致性校验的第二意见，不参与金额落库；key 仅存本地数据库、不回传明文。"
      />

      <Card>
        <div className="hidden md:block">
          <Table
            rowKey="id"
            columns={columns}
            dataSource={providers}
            loading={isLoading}
            pagination={false}
            scroll={{ x: isWide ? 1150 : 600 }}
            rowClassName={(record) => (record.is_default ? 'bg-amber-50' : '')}
            locale={{ emptyText: '暂无供应商，点击右上角「新增供应商」添加' }}
          />
        </div>
        <div className="divide-y divide-line md:hidden">
          {providers.length === 0 ? (
            <p className="py-8 text-center text-body text-muted">暂无供应商，点击右上角「新增供应商」添加</p>
          ) : providers.map((record) => {
            const testing = testMutation.isPending && testMutation.variables?.id === record.id
            return (
              <div key={record.id} className={record.is_default ? 'space-y-3 bg-status-warn-surface/60 px-3 py-4' : 'space-y-3 px-3 py-4'}>
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="text-body-strong font-medium text-ink">{record.label}</p>
                      {record.is_default && <span className="rounded-pill bg-status-warn-surface px-2 py-0.5 text-meta text-status-warn-text">默认</span>}
                      {/* 测试状态徽章（票 02）：与桌面表格同一组件，状态两处必然一致 */}
                      <TestStatusBadge record={record} />
                    </div>
                    <p className="mt-1 text-meta text-muted">{kindShortLabel(record.kind)} · {record.model}</p>
                  </div>
                  <Switch checked={record.enabled} onChange={(v) => toggleEnabled(record, v)} aria-label={record.enabled ? `停用${record.label}` : `启用${record.label}`} />
                </div>
                <dl className="grid grid-cols-1 gap-2 text-meta">
                  <div className="min-w-0"><dt className="text-muted">Base URL</dt><dd className="break-all text-secondary">{record.base_url}</dd></div>
                  <div><dt className="text-muted">API Key</dt><dd className="pf-num text-secondary">{record.api_key_masked || '—'}</dd></div>
                </dl>
                <div className="grid grid-cols-2 gap-2">
                  <button type="button" disabled={testing || (testMutation.isPending && !testing)} onClick={() => testMutation.mutate({ id: record.id, label: record.label })} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-2 text-meta font-medium text-ink disabled:opacity-50">
                    {testing ? '测试中…' : '测试'}
                  </button>
                  <button type="button" onClick={() => openEdit(record)} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-2 text-meta font-medium text-ink">编辑</button>
                  {/* 与桌面操作列同一条互补规则：默认行给「取消默认」，其余行仍是「设为默认」 */}
                  {record.is_default ? (
                    <Popconfirm
                      title="取消默认供应商？"
                      description="取消后不会自动使用任何供应商：截图一致性校验会标为「未校验」，投资建议会提示先设置默认供应商。"
                      okText="取消默认"
                      cancelText="保留默认"
                      onConfirm={() => unmarkDefault(record)}
                    >
                      <button type="button" className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-2 text-meta font-medium text-ink">取消默认</button>
                    </Popconfirm>
                  ) : (
                    <button type="button" onClick={() => markDefault(record)} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-2 text-meta font-medium text-ink">设为默认</button>
                  )}
                  <button type="button" onClick={() => setDeleting(record)} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-status-critical/30 bg-surface px-2 text-meta font-medium text-status-critical-text">删除</button>
                </div>
              </div>
            )
          })}
        </div>
      </Card>

      <Modal
        title={editing ? '编辑供应商' : '新增供应商'}
        open={modalOpen}
        onOk={handleSubmit}
        onCancel={close}
        okText={editing ? '保存修改' : '创建'}
        cancelText="取消"
        confirmLoading={createMutation.isPending || updateMutation.isPending}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" className="mt-4">
          <Form.Item
            name="label"
            label="显示名称"
            rules={[{ required: true, message: '请输入显示名称' }]}
          >
            <Input placeholder="如：DeepSeek 主账号" />
          </Form.Item>
          <Form.Item
            name="kind"
            label="供应商类型"
            rules={[{ required: true, message: '请选择供应商类型' }]}
          >
            <Select options={KIND_OPTIONS} placeholder="选择类型" />
          </Form.Item>
          <Form.Item
            name="base_url"
            label="Base URL"
            rules={[{ required: true, message: '请输入 Base URL' }]}
            extra="DeepSeek 如 https://api.deepseek.com/v1；DashScope 如 https://dashscope.aliyuncs.com/compatible-mode/v1"
          >
            <Input placeholder="https://api.example.com/v1" />
          </Form.Item>
          <Form.Item
            name="model"
            label="模型"
            rules={[{ required: true, message: '请输入模型名称' }]}
          >
            <Input placeholder="如 deepseek-chat、qwen-plus" />
          </Form.Item>
          <Form.Item
            name="api_key"
            label="API Key"
            rules={editing ? [] : [{ required: true, message: '请输入 API Key' }]}
            extra={editing ? '留空表示不修改已保存的 key' : undefined}
          >
            <Input.Password
              placeholder={editing ? '不修改请留空' : '仅存本地数据库，不回传明文'}
              autoComplete="new-password"
            />
          </Form.Item>
          <Form.Item name="enabled" label="启用" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item
            name="is_default"
            label="设为默认"
            valuePropName="checked"
            extra="校验未指定供应商时使用默认项"
          >
            <Switch />
          </Form.Item>
        </Form>
      </Modal>

      {/* 删除确认走 ConfirmDialog（原 Popconfirm 没有理由槽）：缺理由时确认按钮不出请求 */}
      <ConfirmDialog
        open={!!deleting}
        onClose={() => {
          setDeleting(null)
          setDeleteError(null)
        }}
        onConfirm={() => deleting && deleteMutation.mutate({
          id: deleting.id,
          audit: toAuditReasonInput(deleteReason),
        })}
        title={deleting ? `删除供应商「${deleting.label}」？` : '删除供应商？'}
        description="删除后不可恢复；若删的是默认供应商，后端会把下一家启用的供应商提为默认。"
        confirmLabel="删除供应商"
        danger
        loading={deleteMutation.isPending}
      >
        <div className="space-y-3">
          {deleteError && <InlineError>{deleteError}</InlineError>}
          <AuditReasonField
            label="为什么删"
            value={deleteReason}
            onChange={setDeleteReason}
            hint="删除会留下谁在何时删了哪一家供应商（不含 key 明文）。"
          />
        </div>
      </ConfirmDialog>
    </div>
  )
}
