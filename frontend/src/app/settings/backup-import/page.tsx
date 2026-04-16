'use client'

import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import {
  Card,
  Tabs,
  Button,
  Upload,
  Table,
  Checkbox,
  Select,
  Alert,
  Space,
  Typography,
  Row,
  Col,
  Tag,
  message,
  Steps
} from 'antd'
import {
  UploadOutlined,
  DownloadOutlined,
  DatabaseOutlined,
  TableOutlined,
  ArrowRightOutlined,
  CheckCircleOutlined,
  ExclamationCircleOutlined
} from '@ant-design/icons'


const { Text } = Typography
const { Option } = Select

// 支持的表列表
const SUPPORTED_TABLES = [
  { key: 'accounts', label: '账户', category: 'config' },
  { key: 'fund_types', label: '资产类型', category: 'config' },
  { key: 'liquidity_ratings', label: '流动性评级', category: 'config' },
  { key: 'asset_owners', label: '资产拥有人', category: 'config' },
  { key: 'alert_rules', label: '预警规则', category: 'config' },
  { key: 'allocation_targets', label: '资产配置目标', category: 'config' },
  { key: 'asset_records', label: '资产记录', category: 'data' },
  { key: 'auto_export_rules', label: '自动导出规则', category: 'config' },
]

// 冲突处理策略
const CONFLICT_STRATEGIES = [
  { value: 'skip', label: '跳过', description: '保留现有数据，跳过重复记录' },
  { value: 'overwrite', label: '覆盖', description: '用新数据替换现有数据' },
  { value: 'merge', label: '合并', description: '智能合并数据（如金额累加）' },
]

interface TableInfo {
  name: string
  row_count: number
  columns: string[]
}

interface StructureDiff {
  new_columns: string[]
  missing_columns: string[]
  type_mismatches: { column: string; source_type: string; target_type: string }[]
}

interface ImportAnalysis {
  tables: TableInfo[]
  structure_diffs: Record<string, StructureDiff>
}

interface ImportPreview {
  sample_data: unknown[]
  preview_rows?: { data: unknown }[]  // 数据库导入返回的格式
  total_count?: number
  total_rows?: number  // 数据库导入返回的格式
  conflict_count: number
}

interface ImportResult {
  success: boolean
  imported_count: number
  skipped_count: number
  overwritten_count: number
  error_count: number
  message: string
}

export default function BackupImportPage() {
  const queryClient = useQueryClient()
  const [activeTab, setActiveTab] = useState('export')
  
  // 导出相关状态
  const [selectedTables, setSelectedTables] = useState<string[]>([])
  const [exportFormat, setExportFormat] = useState<'json' | 'csv'>('json')
  
  // 导入相关状态
  const [tempFileId, setTempFileId] = useState<string>('')
  const [fileType, setFileType] = useState<'db' | 'csv' | 'json'>('db')
  const [analysisResult, setAnalysisResult] = useState<ImportAnalysis | null>(null)
  const [selectedImportTables, setSelectedImportTables] = useState<string[]>([])
  const [conflictStrategy, setConflictStrategy] = useState<string>('skip')
  const [previewData, setPreviewData] = useState<Record<string, ImportPreview>>({})
  const [importResult, setImportResult] = useState<ImportResult | null>(null)
  const [importing, setImporting] = useState(false)
  const [currentStep, setCurrentStep] = useState(0)

  // 查询导出历史
  const { data: exportHistory } = useQuery({
    queryKey: ['exports'],
    queryFn: () => api.listExports(),
    enabled: activeTab === 'export',
  })

  // 导出表数据
  const exportMutation = useMutation({
    mutationFn: () => api.exportTables(selectedTables, exportFormat),
    onSuccess: (data) => {
      message.success('导出成功')
      // 下载文件
      data.files.forEach((file: { filename: string }) => {
        api.downloadExport(file.filename)
      })
      queryClient.invalidateQueries({ queryKey: ['exports'] })
    },
    onError: () => {
      message.error('导出失败')
    },
  })

  // 上传并分析文件
  const uploadMutation = useMutation({
    mutationFn: (file: File) => {
      const extension = file.name.split('.').pop()?.toLowerCase()
      setFileType(extension === 'csv' ? 'csv' : extension === 'json' ? 'json' : 'db')
      if (extension === 'csv' || extension === 'json') {
        return api.uploadDataFileForImport(file)
      }
      return api.uploadDatabaseForImport(file)
    },
    onSuccess: (data) => {
      setAnalysisResult(data)
      setTempFileId(data.temp_file_id || '')
      setCurrentStep(1)
      message.success('文件分析完成')
    },
    onError: () => {
      message.error('文件分析失败')
    },
  })

  // 预览导入数据
  const previewMutation = useMutation({
    mutationFn: async () => {
      const previews: Record<string, ImportPreview> = {}
      for (const tableName of selectedImportTables) {
        let result
        if (fileType === 'csv' || fileType === 'json') {
          result = await api.previewDataFileImport(tempFileId, tableName)
        } else {
          result = await api.previewImport(tempFileId, tableName)
        }
        previews[tableName] = result
      }
      return previews
    },
    onSuccess: (data) => {
      setPreviewData(data)
      setCurrentStep(2)
      message.success('预览数据加载完成')
    },
    onError: () => {
      message.error('预览数据加载失败')
    },
  })

  // 执行导入
  const importMutation = useMutation({
    mutationFn: () => {
      if (fileType === 'csv' || fileType === 'json') {
        // CSV/JSON 文件一次只能导入一个表
        return api.executeDataFileImport(
          tempFileId,
          selectedImportTables[0],
          conflictStrategy
        )
      }
      return api.executeImport(
        tempFileId,
        selectedImportTables,
        conflictStrategy
      )
    },
    onSuccess: (data) => {
      setImportResult(data)
      setImporting(false)
      setCurrentStep(4)  // 导入完成，高亮"执行导入"步骤
      if (data.success) {
        message.success('导入完成')
      } else {
        message.warning(`导入完成但有错误：${data.message}`)
      }
    },
    onError: (error: Error) => {
      setImporting(false)
      message.error(`导入失败：${error.message}`)
    },
  })

  // 处理导出
  const handleExport = () => {
    if (selectedTables.length === 0) {
      message.warning('请至少选择一个表')
      return
    }
    exportMutation.mutate()
  }

  // 处理导入
  const handleImport = () => {
    if (selectedImportTables.length === 0) {
      message.warning('请至少选择一个表')
      return
    }
    setImporting(true)
    importMutation.mutate()
  }

  // 渲染导出页面
  const renderExportTab = () => (
    <Space direction="vertical" style={{ width: '100%' }} size="large">
      <Card title="导出选项" bordered={false}>
        <Space direction="vertical" style={{ width: '100%' }}>
          <div>
            <Text strong>导出格式：</Text>
            <Select
              value={exportFormat}
              onChange={setExportFormat}
              style={{ width: 200, marginLeft: 16 }}
            >
              <Option value="json">JSON（包含结构）</Option>
              <Option value="csv">CSV（仅数据）</Option>
            </Select>
          </div>
          
          <div>
            <Text strong>选择要导出的表：</Text>
            <div style={{ marginTop: 16 }}>
              <Row gutter={[16, 8]}>
                {SUPPORTED_TABLES.map((table) => (
                  <Col span={8} key={table.key}>
                    <Checkbox
                      checked={selectedTables.includes(table.key)}
                      onChange={(e) => {
                        if (e.target.checked) {
                          setSelectedTables([...selectedTables, table.key])
                        } else {
                          setSelectedTables(selectedTables.filter(t => t !== table.key))
                        }
                      }}
                    >
                      {table.label}
                      <Tag color={table.category === 'config' ? 'blue' : 'green'} style={{ marginLeft: 8 }}>
                        {table.category === 'config' ? '配置' : '数据'}
                      </Tag>
                    </Checkbox>
                  </Col>
                ))}
              </Row>
            </div>
          </div>
          
          <Button
            type="primary"
            icon={<DownloadOutlined />}
            onClick={handleExport}
            loading={exportMutation.isPending}
            disabled={selectedTables.length === 0}
          >
            导出选中表
          </Button>
        </Space>
      </Card>

      <Card title="导出历史" bordered={false}>
        <Table
          dataSource={exportHistory?.files || []}
          rowKey="filename"
          columns={[
            { title: '文件名', dataIndex: 'filename' },
            { title: '大小', dataIndex: 'file_size', render: (size: number) => `${(size / 1024).toFixed(2)} KB` },
            { title: '创建时间', dataIndex: 'created_at' },
            {
              title: '操作',
              render: (_, record: { filename: string }) => (
                <Button
                  type="link"
                  icon={<DownloadOutlined />}
                  onClick={() => api.downloadExport(record.filename)}
                >
                  下载
                </Button>
              ),
            },
          ]}
        />
      </Card>
    </Space>
  )

  // 渲染导入步骤
  const renderImportSteps = () => {
    const steps = [
      { title: '上传数据库', description: '选择旧数据库文件' },
      { title: '分析结构', description: '检测表结构差异' },
      { title: '选择表', description: '选择要导入的表' },
      { title: '预览数据', description: '查看将要导入的数据' },
      { title: '执行导入', description: '完成数据迁移' },
    ]

    return (
      <div style={{ marginBottom: 24 }}>
        <Steps
          current={currentStep}
          items={steps.map((step) => ({
            title: step.title,
            description: step.description,
          }))}
        />
      </div>
    )
  }

  // 渲染导入页面
  const renderImportTab = () => (
    <Space direction="vertical" style={{ width: '100%' }} size="large">
      {renderImportSteps()}

      {/* 步骤 0: 上传文件 */}
      {currentStep === 0 && (
        <Card title="上传数据文件" bordered={false}>
          <Alert
            message="支持导入 .db (SQLite数据库)、.csv 或 .json 格式的文件"
            description="CSV/JSON 文件通常用于导入之前导出的数据"
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
          />
          <Upload.Dragger
            beforeUpload={(file) => {
              console.log('File selected:', file.name)
              return false  // 阻止自动上传
            }}
            onChange={(info) => {
              console.log('Upload onChange:', info)
              if (info.file.status === 'done' || info.fileList.length > 0) {
                const file = info.fileList[0]
                if (file.originFileObj) {
                  console.log('Uploading file:', file.name)
                  uploadMutation.mutate(file.originFileObj)
                }
              }
            }}
            accept=".db,.csv,.json"
            maxCount={1}
            customRequest={() => {}}  // 禁用默认上传行为
          >
            <p className="ant-upload-drag-icon">
              <DatabaseOutlined />
            </p>
            <p className="ant-upload-text">点击或拖拽数据库文件到此区域</p>
            <p className="ant-upload-hint">支持 .db、.csv 或 .json 格式的文件</p>
          </Upload.Dragger>
        </Card>
      )}

      {/* 步骤 1: 分析结果 */}
      {currentStep === 1 && analysisResult && (
        <Card title="数据库分析结果" bordered={false}>
          <Alert
            message="分析完成"
            description={`检测到 ${analysisResult.tables.length} 个表，请选择要导入的表`}
            type="success"
            showIcon
            style={{ marginBottom: 16 }}
          />
          
          <Table
            dataSource={analysisResult.tables}
            rowKey="name"
            rowSelection={{
              type: 'checkbox',
              onChange: (selectedRowKeys) => {
                setSelectedImportTables(selectedRowKeys as string[])
              },
            }}
            columns={[
              { title: '表名', dataIndex: 'name' },
              { title: '记录数', dataIndex: 'row_count' },
              { title: '字段', dataIndex: 'columns', render: (cols: string[]) => cols.join(', ') },
              {
                title: '结构差异',
                render: (_, record: TableInfo) => {
                  const diff = analysisResult.structure_diffs[record.name]
                  if (!diff) return <Tag>无差异</Tag>
                  return (
                    <Space>
                      {diff.new_columns.length > 0 && (
                        <Tag color="green">+{diff.new_columns.length} 字段</Tag>
                      )}
                      {diff.missing_columns.length > 0 && (
                        <Tag color="red">-{diff.missing_columns.length} 字段</Tag>
                      )}
                    </Space>
                  )
                },
              },
            ]}
          />
          
          <Button
            type="primary"
            onClick={() => previewMutation.mutate()}
            loading={previewMutation.isPending}
            disabled={selectedImportTables.length === 0}
            style={{ marginTop: 16 }}
          >
            下一步：预览数据
          </Button>
        </Card>
      )}

      {/* 步骤 2: 预览数据 */}
      {currentStep === 2 && Object.keys(previewData).length > 0 && (
        <Card title="数据预览" bordered={false}>
          <Alert
            message="数据预览"
            description="请确认数据无误后执行导入"
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
          />
          
          {selectedImportTables.map((tableName) => {
            const preview = previewData[tableName]
            if (!preview) return null

            // 兼容两种数据格式：CSV/JSON 使用 sample_data，数据库导入使用 preview_rows
            const sampleData = preview.sample_data || preview.preview_rows?.map((r: { data: unknown }) => r.data) || []
            const totalCount = preview.total_count || preview.total_rows || 0

            if (sampleData.length === 0) return null

            return (
              <Card
                key={tableName}
                title={
                  <Space>
                    <TableOutlined />
                    {tableName}
                    <Tag>共 {totalCount} 条</Tag>
                    {preview.conflict_count > 0 && (
                      <Tag color="orange">{preview.conflict_count} 条冲突</Tag>
                    )}
                  </Space>
                }
                style={{ marginBottom: 16 }}
                size="small"
              >
                <Table
                  dataSource={sampleData}
                  columns={Object.keys(sampleData[0] || {}).map(key => ({
                    title: key,
                    dataIndex: key,
                  }))}
                  pagination={false}
                  size="small"
                />
              </Card>
            )
          })}
          
          <Card title="冲突处理策略" size="small" style={{ marginBottom: 16 }}>
            <Select
              value={conflictStrategy}
              onChange={setConflictStrategy}
              style={{ width: 300 }}
            >
              {CONFLICT_STRATEGIES.map((strategy) => (
                <Option key={strategy.value} value={strategy.value}>
                  {strategy.label} - {strategy.description}
                </Option>
              ))}
            </Select>
          </Card>
          
          <Button
            type="primary"
            onClick={handleImport}
            loading={importing}
            disabled={selectedImportTables.length === 0}
          >
            执行导入
          </Button>
        </Card>
      )}

      {/* 步骤 4: 导入结果 */}
      {currentStep === 4 && importResult && (
        <Card title="导入结果" bordered={false}>
          <Alert
            message={importResult.success ? '导入成功' : '导入完成（有错误）'}
            type={importResult.success ? 'success' : 'warning'}
            showIcon
            description={importResult.message}
            style={{ marginBottom: 16 }}
          />
          
          <Row gutter={16}>
            <Col span={6}>
              <Card>
                <Statistic
                  title="新导入"
                  value={importResult.imported_count}
                  valueStyle={{ color: '#3f8600' }}
                  prefix={<CheckCircleOutlined />}
                />
              </Card>
            </Col>
            <Col span={6}>
              <Card>
                <Statistic
                  title="跳过"
                  value={importResult.skipped_count}
                  valueStyle={{ color: '#faad14' }}
                  prefix={<ExclamationCircleOutlined />}
                />
              </Card>
            </Col>
            <Col span={6}>
              <Card>
                <Statistic
                  title="覆盖"
                  value={importResult.overwritten_count || 0}
                  valueStyle={{ color: '#1890ff' }}
                  prefix={<ArrowRightOutlined />}
                />
              </Card>
            </Col>
            <Col span={6}>
              <Card>
                <Statistic
                  title="错误"
                  value={importResult.error_count}
                  valueStyle={{ color: '#cf1322' }}
                  prefix={<CloseCircleOutlined />}
                />
              </Card>
            </Col>
          </Row>
          
          <Button
            type="primary"
            onClick={() => {
              setCurrentStep(0)
              setAnalysisResult(null)
              setSelectedImportTables([])
              setPreviewData({})
              setImportResult(null)
            }}
            style={{ marginTop: 16 }}
          >
            继续导入
          </Button>
        </Card>
      )}
    </Space>
  )

  return (
    <div className="space-y-6 animate-fade-in">
      <div>
        <h1 className="text-2xl font-bold text-brand-950">数据备份与导入</h1>
        <p className="text-sm text-slate-500 mt-1">
          导出数据库备份或从旧数据库导入数据
        </p>
      </div>

      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          {
            key: 'export',
            label: '数据导出',
            icon: <DownloadOutlined />,
            children: renderExportTab(),
          },
          {
            key: 'import',
            label: '数据导入',
            icon: <UploadOutlined />,
            children: renderImportTab(),
          },
        ]}
      />
    </div>
  )
}

// 辅助组件
function Statistic({ title, value, valueStyle, prefix }: {
  title: string
  value: number
  valueStyle?: React.CSSProperties
  prefix?: React.ReactNode
}) {
  return (
    <div style={{ textAlign: 'center' }}>
      <div style={{ fontSize: 14, color: '#666' }}>{title}</div>
      <div style={{ fontSize: 32, fontWeight: 'bold', marginTop: 8, ...valueStyle }}>
        {prefix}
        {value}
      </div>
    </div>
  )
}

function CloseCircleOutlined() {
  return <span style={{ color: '#cf1322' }}>✕</span>
}
