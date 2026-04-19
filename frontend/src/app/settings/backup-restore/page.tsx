'use client'

import { useState, useEffect } from 'react'
import { Button, Card, Table, Upload, message, Modal, Space, Typography, Alert, Spin, InputNumber, Radio, Switch, Tooltip } from 'antd'
import {
  DownloadOutlined,
  DeleteOutlined,
  UploadOutlined,
  DatabaseOutlined,
  WarningOutlined,
  HistoryOutlined,
  FileOutlined,
  SettingOutlined,
  InfoCircleOutlined
} from '@ant-design/icons'
import type { UploadFile } from 'antd/es/upload/interface'
import { format } from 'date-fns'
import { zhCN } from 'date-fns/locale'

const { Title, Text } = Typography

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
  const [uploadFileList, setUploadFileList] = useState<UploadFile[]>([])
  const [selectedBackup, setSelectedBackup] = useState<BackupFile | null>(null)
  const [restoreModalVisible, setRestoreModalVisible] = useState(false)
  const [uploadRestoreModalVisible, setUploadRestoreModalVisible] = useState(false)
  const [policyModalVisible, setPolicyModalVisible] = useState(false)
  const [policy, setPolicy] = useState<RetentionPolicy>({
    enabled: false,
    type: 'count',
    count: 10,
    days: 30
  })

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
      const response = await fetch('http://localhost:8000/api/backup/list')
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

  // 格式化文件大小
  const formatFileSize = (bytes: number): string => {
    if (bytes === 0) return '0 B'
    const k = 1024
    const sizes = ['B', 'KB', 'MB', 'GB']
    const i = Math.floor(Math.log(bytes) / Math.log(k))
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i]
  }

  // 格式化日期
  const formatDate = (dateStr: string): string => {
    try {
      return format(new Date(dateStr), 'yyyy-MM-dd HH:mm:ss', { locale: zhCN })
    } catch {
      return dateStr
    }
  }

  // 应用保留策略
  const applyRetentionPolicy = async (currentBackups: BackupFile[]) => {
    if (!policy.enabled || currentBackups.length === 0) return

    let backupsToDelete: BackupFile[] = []

    if (policy.type === 'count') {
      // 保留最近N个备份
      if (currentBackups.length > policy.count) {
        backupsToDelete = currentBackups.slice(policy.count)
      }
    } else {
      // 保留最近N天的备份
      const cutoffDate = new Date()
      cutoffDate.setDate(cutoffDate.getDate() - policy.days)
      backupsToDelete = currentBackups.filter(backup => 
        new Date(backup.created_at) < cutoffDate
      )
    }

    // 排除回滚备份（文件名包含 rollback）
    backupsToDelete = backupsToDelete.filter(backup => 
      !backup.filename.includes('rollback')
    )

    if (backupsToDelete.length > 0) {
      console.log(`Applying retention policy: deleting ${backupsToDelete.length} old backups`)
      for (const backup of backupsToDelete) {
        try {
          await fetch(`http://localhost:8000/api/backup/delete/${backup.filename}`, {
            method: 'DELETE'
          })
        } catch (error) {
          console.error(`Failed to delete backup ${backup.filename}:`, error)
        }
      }
      // 刷新列表
      loadBackups()
    }
  }

  // 创建备份
  const handleCreateBackup = async () => {
    setCreating(true)
    try {
      const response = await fetch('http://localhost:8000/api/backup/create', {
        method: 'POST'
      })
      const data: BackupResponse = await response.json()
      if (data.success) {
        message.success('备份创建成功')
        await loadBackups()
        // 应用保留策略
        const updatedResponse = await fetch('http://localhost:8000/api/backup/list')
        const updatedData = await updatedResponse.json()
        if (updatedData.success && updatedData.backups) {
          applyRetentionPolicy(updatedData.backups)
        }
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
    window.open(`http://localhost:8000/api/backup/download/${filename}`, '_blank')
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
          const response = await fetch(`http://localhost:8000/api/backup/delete/${filename}`, {
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

      const response = await fetch('http://localhost:8000/api/backup/restore', {
        method: 'POST',
        body: formData
      })
      const data: BackupResponse = await response.json()
      if (data.success) {
        message.success('数据库恢复成功，请刷新页面')
        setRestoreModalVisible(false)
        setSelectedBackup(null)
        // 恢复成功后刷新备份列表（恢复操作会创建回滚备份）
        await loadBackups()
        // 应用保留策略
        const updatedResponse = await fetch('http://localhost:8000/api/backup/list')
        const updatedData = await updatedResponse.json()
        if (updatedData.success && updatedData.backups) {
          applyRetentionPolicy(updatedData.backups)
        }
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

  // 从上传文件恢复
  const handleUploadRestore = async () => {
    if (uploadFileList.length === 0) {
      message.error('请选择备份文件')
      return
    }

    const file = uploadFileList[0]
    if (!file.originFileObj) {
      message.error('文件对象无效')
      return
    }

    setRestoring(true)
    try {
      const formData = new FormData()
      formData.append('file', file.originFileObj)

      const response = await fetch('http://localhost:8000/api/backup/restore', {
        method: 'POST',
        body: formData
      })
      const data: BackupResponse = await response.json()
      if (data.success) {
        message.success('数据库恢复成功，请刷新页面')
        setUploadRestoreModalVisible(false)
        setUploadFileList([])
        // 恢复成功后刷新备份列表（恢复操作会创建回滚备份）
        await loadBackups()
        // 应用保留策略
        const updatedResponse = await fetch('http://localhost:8000/api/backup/list')
        const updatedData = await updatedResponse.json()
        if (updatedData.success && updatedData.backups) {
          applyRetentionPolicy(updatedData.backups)
        }
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
    message.success('保留策略已保存')
    setPolicyModalVisible(false)
    // 立即应用策略
    applyRetentionPolicy(backups)
  }

  const columns = [
    {
      title: '文件名',
      dataIndex: 'filename',
      key: 'filename',
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
      render: (date: string) => formatDate(date)
    },
    {
      title: '操作',
      key: 'action',
      width: 250,
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
      <div className="flex items-center justify-between">
        <Title level={2} className="!mb-0">
          <DatabaseOutlined className="mr-2" />
          备份与恢复
        </Title>
        <Button
          icon={<SettingOutlined />}
          onClick={() => setPolicyModalVisible(true)}
        >
          保留策略
          {policy.enabled && <span className="ml-1 text-green-500">●</span>}
        </Button>
      </div>

      {/* 警告提示 */}
      <Alert
        message="重要提示"
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
          message={
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
            onClick={() => setUploadRestoreModalVisible(true)}
          >
            上传备份并恢复
          </Button>
        </Space>
      </Card>

      {/* 备份列表 */}
      <Card title="备份文件列表" loading={loading}>
        <Table
          columns={columns}
          dataSource={backups}
          rowKey="filename"
          pagination={false}
          locale={{ emptyText: '暂无备份文件' }}
        />
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
        maskClosable={!restoring}
      >
        <Alert
          message="警告：此操作不可撤销！"
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

      {/* 上传并恢复对话框 */}
      <Modal
        title={
          <Space>
            <UploadOutlined />
            <span>上传备份文件并恢复</span>
          </Space>
        }
        open={uploadRestoreModalVisible}
        onOk={handleUploadRestore}
        onCancel={() => {
          if (!restoring) {
            setUploadRestoreModalVisible(false)
            setUploadFileList([])
          }
        }}
        okText="上传并恢复"
        cancelText="取消"
        okButtonProps={{ danger: true, disabled: uploadFileList.length === 0, loading: restoring }}
        cancelButtonProps={{ disabled: restoring }}
        closable={!restoring}
        maskClosable={!restoring}
      >
        <Space direction="vertical" size="large" style={{ width: '100%' }}>
          <Alert
            message="警告：此操作不可撤销！"
            description="上传的备份文件将完全替换当前数据库，所有现有数据将被覆盖。"
            type="warning"
            showIcon
          />
          <Upload.Dragger
            beforeUpload={(file) => {
              const isDbFile = file.name?.endsWith('.db') || file.name?.endsWith('.sqlite')
              if (!isDbFile) {
                message.error('只支持 .db 或 .sqlite 格式的备份文件')
                return false
              }
              return false
            }}
            onChange={(info) => {
              setUploadFileList(info.fileList)
            }}
            maxCount={1}
            fileList={uploadFileList}
            disabled={restoring}
          >
            <p className="ant-upload-drag-icon">
              <DatabaseOutlined />
            </p>
            <p className="ant-upload-text">点击或拖拽备份文件到此处</p>
            <p className="ant-upload-hint">支持 .db 或 .sqlite 格式的 SQLite 数据库文件</p>
          </Upload.Dragger>
        </Space>
      </Modal>

      {/* 保留策略设置对话框 */}
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
            message="说明"
            description="设置备份保留策略后，系统会在创建新备份或恢复数据库时自动清理超出保留范围的旧备份（回滚备份除外）。"
            type="info"
            showIcon
          />
          
          <div>
            <div className="flex items-center justify-between mb-4">
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
                  message="提示"
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
