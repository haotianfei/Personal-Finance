'use client'

import { useState, useEffect } from 'react'
import { Button, Card, Table, Upload, message, Modal, Space, Typography, Alert, Spin, InputNumber, Radio, Switch } from 'antd'
import {
  DownloadOutlined,
  DeleteOutlined,
  UploadOutlined,
  DatabaseOutlined,
  WarningOutlined,
  HistoryOutlined,
  FileOutlined,
  SettingOutlined,
} from '@ant-design/icons'
import type { UploadFile } from 'antd/es/upload/interface'
import { format } from 'date-fns'
import { zhCN } from 'date-fns/locale'
import { api, newIdemKey } from '@/lib/api'
import { PageHeader } from '@/components/layout/PageHeader'
import type { ColumnsType } from 'antd/es/table'
import { formatFileSize } from '@/lib/format'

const { Text } = Typography

// v5 信封：backup 走原生 fetch，非 2xx 时读统一错误信封（无信封回退旧 detail）
async function readBackupError(res: Response): Promise<string> {
  const body = await res.json().catch(() => ({}))
  if (body?.error?.message) return body.error.message
  if (typeof body?.detail === 'string') return body.detail
  return `请求失败（HTTP ${res.status}）`
}

interface BackupFile {
  filename: string
  size: number
  created_at: string
}

interface BackupResponse {
  success: boolean
  message?: string
  backup?: BackupFile
  backups?: BackupFile[]
  warnings?: string[]
}

type RetentionType = 'count' | 'days'

interface RetentionPolicy {
  enabled: boolean
  type: RetentionType
  count: number
  days: number
}

const POLICY_STORAGE_KEY = 'backup_retention_policy'

export default function BackupRestorePage() {
  const [backups, setBackups] = useState<BackupFile[]>([])
  const [loading, setLoading] = useState(false)
  const [creating, setCreating] = useState(false)
  const [restoring, setRestoring] = useState(false)
  const [selectedBackup, setSelectedBackup] = useState<BackupFile | null>(null)
  const [restoreModalVisible, setRestoreModalVisible] = useState(false)
  const [policyModalVisible, setPolicyModalVisible] = useState(false)
  const [policy, setPolicy] = useState<RetentionPolicy>({
    enabled: false,
    type: 'count',
    count: 10,
    days: 30
  })

  // 统一上传数据库相关状态
  const [uploadDbModalVisible, setUploadDbModalVisible] = useState(false)
  const [uploadDbFileList, setUploadDbFileList] = useState<UploadFile[]>([])
  const [uploadDbProcessing, setUploadDbProcessing] = useState(false)
  const [uploadDbResult, setUploadDbResult] = useState<{
    action: 'restore'
    success: boolean
    message: string
    warnings?: string[]
  } | null>(null)

  // 加载保留策略
  useEffect(() => {
    const savedPolicy = localStorage.getItem(POLICY_STORAGE_KEY)
    if (savedPolicy) {
      try {
        setPolicy(JSON.parse(savedPolicy))
      } catch {
        console.error('Failed to parse retention policy')
      }
    }
  }, [])

  // 加载备份列表
  const loadBackups = async () => {
    setLoading(true)
    try {
      const response = await fetch('/api/backup/list')
      const data: BackupResponse = await response.json()
      if (data.success && data.backups) {
        setBackups(data.backups)
      }
    } catch (error) {
      message.error('加载备份列表失败')
      console.error(error)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadBackups()
  }, [])

  // 格式化日期
  const formatDate = (dateStr: string): string => {
    try {
      return format(new Date(dateStr), 'yyyy-MM-dd HH:mm:ss', { locale: zhCN })
    } catch {
      return dateStr
    }
  }

  // 保留策略已迁到后端调度器（spec audit-reason-enforce E2①）：策略是系统规则，
  // 不是人的裁决，所以不再由前端循环发 DELETE /api/backup/delete/...。
  // 页面上保留策略的开关/展示照旧，只负责记住用户的偏好；实际清理按后端节奏发生。

  // 创建备份
  const handleCreateBackup = async () => {
    setCreating(true)
    try {
      const response = await fetch('/api/backup/create', {
        method: 'POST',
        headers: { 'Idempotency-Key': newIdemKey() },
      })
      if (!response.ok) {
        message.error(await readBackupError(response))
        return
      }
      const data: BackupResponse = await response.json()
      if (data.success) {
        message.success('备份创建成功')
        // 清理若已发生（后端保留策略），刷新后的列表自然少掉几份；这里只负责重新读。
        await loadBackups()
      } else {
        message.error(data.message || '备份失败')
      }
    } catch (error) {
      message.error('备份创建失败')
      console.error(error)
    } finally {
      setCreating(false)
    }
  }

  // 下载备份
  const handleDownload = (filename: string) => {
    const a = document.createElement('a')
    a.href = `/api/backup/download/${encodeURIComponent(filename)}`
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
  }

  // 删除备份
  const handleDelete = (filename: string) => {
    Modal.confirm({
      title: '确认删除',
      content: `确定要删除备份文件 "${filename}" 吗？此操作不可恢复。`,
      okText: '删除',
      okType: 'danger',
      cancelText: '取消',
      onOk: async () => {
        try {
          const response = await fetch(`/api/backup/delete/${filename}`, {
            method: 'DELETE'
          })
          const data: BackupResponse = await response.json()
          if (data.success) {
            message.success('备份已删除')
            loadBackups()
          } else {
            message.error(data.message || '删除失败')
          }
        } catch (error) {
          message.error('删除备份失败')
          console.error(error)
        }
      }
    })
  }

  // 显示恢复确认对话框
  const showRestoreConfirm = (backup: BackupFile) => {
    setSelectedBackup(backup)
    setRestoreModalVisible(true)
  }

  // 从服务器备份恢复
  const handleRestore = async () => {
    if (!selectedBackup) return

    setRestoring(true)
    try {
      const formData = new FormData()
      formData.append('filename', selectedBackup.filename)

      const response = await fetch('/api/backup/restore', {
        method: 'POST',
        body: formData,
        headers: { 'Idempotency-Key': newIdemKey() },
      })
      if (!response.ok) {
        message.error(await readBackupError(response))
        return
      }
      const data: BackupResponse = await response.json()
      if (data.success) {
        message.success('数据库恢复成功，请刷新页面')
        // v5：旧裸 .db 恢复警示（不含凭证）——非错误，成功体内 warnings[]
        for (const w of data.warnings ?? []) message.warning(w)
        setRestoreModalVisible(false)
        setSelectedBackup(null)
        // 恢复成功后刷新备份列表（恢复操作会创建回滚备份）
        await loadBackups()
      } else {
        message.error(data.message || '恢复失败')
      }
    } catch (error) {
      message.error('数据库恢复失败')
      console.error(error)
    } finally {
      setRestoring(false)
    }
  }

  // 保存保留策略
  const savePolicy = () => {
    localStorage.setItem(POLICY_STORAGE_KEY, JSON.stringify(policy))
    // 说清楚什么时候生效：清理由后端调度器按这个偏好执行，这次点击本身不删文件
    message.success('保留策略已保存；实际清理由后台按此策略执行')
    setPolicyModalVisible(false)
  }

  // 统一上传数据库：执行上传并自动处理
  const handleUploadDb = async () => {
    if (uploadDbFileList.length === 0) {
      message.error('请选择数据库文件')
      return
    }

    const file = uploadDbFileList[0]
    if (!file.originFileObj) {
      message.error('文件对象无效')
      return
    }

    setUploadDbProcessing(true)
    try {
      const isZip = file.originFileObj.name.toLowerCase().endsWith('.zip')
      // .zip bundle 走 /backup/restore（含凭证合并）；裸 .db 仍走 upload-db（仅库）
      // v5 幂等：每次用户恢复动作一个 uuid（react-query 无关，原生 await 重试即重点击＝新动作）
      const idem = newIdemKey()
      const result = isZip
        ? { action: 'restore' as const, ...await api.restoreBackupFile(file.originFileObj, idem) }
        : await api.uploadDbFile(file.originFileObj, idem)
      setUploadDbResult(result)

      if (result.success) {
        message.success('备份恢复成功！请刷新页面')
      } else {
        message.error(result.message)
      }
    } catch (error: unknown) {
      message.error(error instanceof Error ? error.message : '处理失败')
    } finally {
      setUploadDbProcessing(false)
    }
  }

  // 重置统一上传状态
  const resetUploadDb = () => {
    setUploadDbModalVisible(false)
    setUploadDbFileList([])
    setUploadDbResult(null)
  }

  const columns: ColumnsType<BackupFile> = [
    {
      title: '文件名',
      dataIndex: 'filename',
      key: 'filename',
      width: 260,
      render: (text: string) => (
        <Space>
          <FileOutlined />
          <Text>{text}</Text>
        </Space>
      )
    },
    {
      title: '大小',
      dataIndex: 'size',
      key: 'size',
      width: 120,
      render: (size: number) => formatFileSize(size)
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 180,
      responsive: ['lg'],
      render: (date: string) => formatDate(date)
    },
    {
      title: '操作',
      key: 'action',
      width: 320,
      render: (_: unknown, record: BackupFile) => (
        <Space size="small">
          <Button
            type="primary"
            ghost
            size="small"
            icon={<HistoryOutlined />}
            onClick={() => showRestoreConfirm(record)}
          >
            恢复
          </Button>
          <Button
            size="small"
            icon={<DownloadOutlined />}
            onClick={() => handleDownload(record.filename)}
          >
            下载
          </Button>
          <Button
            danger
            size="small"
            className="!text-status-critical-text"
            icon={<DeleteOutlined />}
            onClick={() => handleDelete(record.filename)}
          >
            删除
          </Button>
        </Space>
      )
    }
  ]

  return (
    <div className="space-y-6">
      <PageHeader
        title="备份与恢复"
        description="数据库备份、恢复、导出预览与保留策略。"
        actions={
          <Button icon={<SettingOutlined />} onClick={() => setPolicyModalVisible(true)}>
            保留策略
            {policy.enabled && <span className="ml-1 text-status-good">●</span>}
          </Button>
        }
      />

      {/* 警告提示 */}
      <Alert
        title="重要提示"
        description={
          <div>
            <p>1. <strong>备份</strong>：创建当前数据库的完整副本，用于灾难恢复</p>
            <p>2. <strong>恢复</strong>：用备份文件替换当前数据库，操作前请确保已备份重要数据</p>
            <p>3. 如需导入 CSV 数据，请使用&quot;数据导入&quot;功能</p>
          </div>
        }
        type="info"
        showIcon
      />

      {/* 保留策略提示 */}
      {policy.enabled && (
        <Alert
          title={
            <Space>
              <span>保留策略已启用</span>
              <span className="text-gray-500">
                {policy.type === 'count' 
                  ? `仅保留最近 ${policy.count} 个备份`
                  : `仅保留最近 ${policy.days} 天的备份`
                }
              </span>
            </Space>
          }
          type="success"
          showIcon
        />
      )}

      {/* 操作按钮 */}
      <Card>
        <Space size="large" wrap>
          <Button
            type="primary"
            icon={<DatabaseOutlined />}
            size="large"
            onClick={handleCreateBackup}
            loading={creating}
          >
            立即备份
          </Button>
          <Button
            icon={<UploadOutlined />}
            size="large"
            onClick={() => setUploadDbModalVisible(true)}
          >
            上传数据库文件
          </Button>
        </Space>
      </Card>

      {/* 备份列表 */}
      <Card title="备份文件列表" loading={loading}>
        <div className="hidden md:block">
          <Table
            columns={columns}
            dataSource={backups}
            rowKey="filename"
            pagination={false}
            scroll={{ x: 720 }}
            locale={{ emptyText: '暂无备份文件' }}
          />
        </div>
        <div className="divide-y divide-line md:hidden">
          {backups.length === 0 ? (
            <p className="py-8 text-center text-body text-muted">暂无备份文件</p>
          ) : backups.map((backup) => (
            <div key={backup.filename} className="space-y-3 py-4">
              <div className="flex items-start justify-between gap-3">
                <p className="min-w-0 break-all text-body-strong font-medium text-ink">{backup.filename}</p>
                <span className="pf-num shrink-0 text-meta text-secondary">{formatFileSize(backup.size)}</span>
              </div>
              <p className="pf-num text-meta text-muted">{formatDate(backup.created_at)}</p>
              <div className="grid grid-cols-3 gap-2">
                <button type="button" onClick={() => showRestoreConfirm(backup)} className="inline-flex min-h-[44px] items-center justify-center gap-1 rounded-control border border-line-strong bg-surface px-2 text-meta font-medium text-ink">
                  <HistoryOutlined aria-hidden /> 恢复
                </button>
                <button type="button" onClick={() => handleDownload(backup.filename)} className="inline-flex min-h-[44px] items-center justify-center gap-1 rounded-control border border-line-strong bg-surface px-2 text-meta font-medium text-ink">
                  <DownloadOutlined aria-hidden /> 下载
                </button>
                <button type="button" onClick={() => handleDelete(backup.filename)} className="inline-flex min-h-[44px] items-center justify-center gap-1 rounded-control border border-status-critical/30 bg-surface px-2 text-meta font-medium text-status-critical-text">
                  <DeleteOutlined aria-hidden /> 删除
                </button>
              </div>
            </div>
          ))}
        </div>
      </Card>

      {/* 恢复确认对话框 - 从服务器备份 */}
      <Modal
        title={
          <Space>
            <WarningOutlined style={{ color: '#faad14' }} />
            <span>确认恢复数据库</span>
          </Space>
        }
        open={restoreModalVisible}
        onOk={handleRestore}
        onCancel={() => {
          setRestoreModalVisible(false)
          setSelectedBackup(null)
        }}
        okText="确认恢复"
        cancelText="取消"
        okButtonProps={{ danger: true, loading: restoring }}
        cancelButtonProps={{ disabled: restoring }}
        closable={!restoring}
        mask={{ closable: !restoring }}
      >
        <Alert
          title="警告：此操作不可撤销！"
          description={
            <div>
              <p>您即将使用备份文件 <strong>{selectedBackup?.filename}</strong> 恢复数据库。</p>
              <p>恢复操作将<strong>完全替换</strong>当前数据库，所有现有数据将被覆盖。</p>
              <p>建议在恢复前先创建当前数据库的备份。</p>
            </div>
          }
          type="warning"
          showIcon
        />
      </Modal>

      {/* 上传数据库文件对话框（统一处理 V1 迁移和 V2 恢复） */}
      <Modal
        title={
          <Space>
            <UploadOutlined />
            <span>上传数据库文件</span>
          </Space>
        }
        open={uploadDbModalVisible}
        onCancel={resetUploadDb}
        footer={null}
        width={600}
        closable={!uploadDbProcessing}
        mask={{ closable: !uploadDbProcessing }}
      >
        <Space direction="vertical" size="large" style={{ width: '100%' }}>
          {!uploadDbResult && !uploadDbProcessing && (
            <>
              <Alert
                title="说明"
                description={
                  <div>
                    <p>上传备份文件（.zip bundle 含凭证 / .db 仅数据库），将恢复并替换当前数据库：</p>
                    <ul className="list-disc pl-5 mt-2">
                      <li><strong>.zip bundle</strong>：恢复数据库并合并凭证（只增不删）</li>
                      <li><strong>.db / .sqlite</strong>：仅恢复数据库（不含凭证）</li>
                      <li>恢复成功后请刷新页面查看最新数据</li>
                    </ul>
                  </div>
                }
                type="info"
                showIcon
              />

              <Upload.Dragger
                beforeUpload={(file) => {
                  const isBackupFile = file.name?.endsWith('.zip') || file.name?.endsWith('.db') || file.name?.endsWith('.sqlite')
                  if (!isBackupFile) {
                    message.error('只支持 .zip bundle 或 .db/.sqlite 格式的备份文件')
                    return false
                  }
                  return false
                }}
                onChange={(info) => {
                  setUploadDbFileList(info.fileList)
                  setUploadDbResult(null)
                }}
                maxCount={1}
                fileList={uploadDbFileList}
                disabled={uploadDbProcessing}
              >
                <p className="ant-upload-drag-icon">
                  <DatabaseOutlined />
                </p>
                <p className="ant-upload-text">点击或拖拽备份文件到此处</p>
                <p className="ant-upload-hint">支持 .zip bundle（含凭证）或 .db/.sqlite 格式的备份文件</p>
              </Upload.Dragger>

              <Button
                type="primary"
                size="large"
                block
                onClick={handleUploadDb}
                loading={uploadDbProcessing}
                disabled={uploadDbFileList.length === 0}
              >
                上传并自动处理
              </Button>
            </>
          )}

          {uploadDbProcessing && (
            <div className="text-center py-8">
              <Spin size="large" />
              <p className="mt-4 text-lg">正在处理，请稍候...</p>
              <p className="text-gray-500">此过程可能需要几分钟，取决于数据量大小</p>
            </div>
          )}

          {uploadDbResult && (
            <>
              {uploadDbResult.success && (
                <Alert
                  title="数据库恢复成功"
                  description="备份文件已成功恢复，请刷新页面查看最新数据。"
                  type="success"
                  showIcon
                />
              )}

              {/* v5：成功体 warnings（如旧裸 .db 不含凭证）——非错误，独立展示 */}
              {uploadDbResult.success && (uploadDbResult.warnings?.length ?? 0) > 0 && (
                <Alert
                  title="恢复警示"
                  description={uploadDbResult.warnings!.join('；')}
                  type="warning"
                  showIcon
                />
              )}

              {!uploadDbResult.success && (
                <Alert
                  title="处理失败"
                  description={uploadDbResult.message}
                  type="error"
                  showIcon
                />
              )}

              <Button type="primary" block onClick={resetUploadDb}>
                刷新页面
              </Button>
            </>
          )}
        </Space>
      </Modal>
      <Modal
        title={
          <Space>
            <SettingOutlined />
            <span>备份保留策略</span>
          </Space>
        }
        open={policyModalVisible}
        onOk={savePolicy}
        onCancel={() => setPolicyModalVisible(false)}
        okText="保存"
        cancelText="取消"
      >
        <Space direction="vertical" size="large" style={{ width: '100%' }}>
          <Alert
            title="说明"
            description="设置备份保留策略后，系统会在创建新备份或恢复数据库时自动清理超出保留范围的旧备份（回滚备份除外）。"
            type="info"
            showIcon
          />
          
          <div>
            <div className="flex flex-wrap items-center justify-between gap-y-2 mb-4">
              <Text strong>启用保留策略</Text>
              <Switch
                checked={policy.enabled}
                onChange={(checked) => setPolicy({ ...policy, enabled: checked })}
              />
            </div>
            
            {policy.enabled && (
              <>
                <Radio.Group
                  value={policy.type}
                  onChange={(e) => setPolicy({ ...policy, type: e.target.value })}
                  className="mb-4"
                >
                  <Radio value="count">
                    <Space>
                      保留最近
                      <InputNumber
                        min={1}
                        max={100}
                        value={policy.count}
                        onChange={(value) => setPolicy({ ...policy, count: value || 10 })}
                        disabled={policy.type !== 'count'}
                      />
                      个备份
                    </Space>
                  </Radio>
                  <Radio value="days" className="mt-2">
                    <Space>
                      保留最近
                      <InputNumber
                        min={1}
                        max={365}
                        value={policy.days}
                        onChange={(value) => setPolicy({ ...policy, days: value || 30 })}
                        disabled={policy.type !== 'days'}
                      />
                      天的备份
                    </Space>
                  </Radio>
                </Radio.Group>
                
                <Alert
                  title="提示"
                  description="回滚备份（文件名包含 rollback）不会被自动删除，以确保数据安全。"
                  type="warning"
                  showIcon
                />
              </>
            )}
          </div>
        </Space>
      </Modal>

      {/* 恢复中遮罩 */}
      {restoring && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-8 flex flex-col items-center">
            <Spin size="large" />
            <p className="mt-4 text-lg">正在恢复数据库，请稍候...</p>
            <p className="text-gray-500">恢复完成后请刷新页面</p>
          </div>
        </div>
      )}
    </div>
  )
}
